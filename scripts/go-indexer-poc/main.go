// Go indexer canary: claim → download → complete via Python ingest.
// Requires Settings toggle go_indexer_enabled=true on the API.
//
// Local:
//   go run . -n 10 -api http://127.0.0.1:8002 -parallel 2
//
// Production canary (after toggle ON in Settings):
//   go run . -n 20 -api https://dfi-backend-production.up.railway.app -parallel 2
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

type claimItem struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	MimeType string `json:"mime_type"`
	Size     *int64 `json:"size"`
	Path     string `json:"path"`
}

type claimResponse struct {
	Enabled      bool        `json:"enabled"`
	Items        []claimItem `json:"items"`
	MaxParallel  int         `json:"max_parallel"`
	CanaryLimit  int         `json:"canary_limit"`
}

type goStatus struct {
	Enabled         bool    `json:"enabled"`
	Alive           bool    `json:"alive"`
	LastFilesPerSec float64 `json:"last_files_per_sec"`
}

func main() {
	n := flag.Int("n", 10, "max files to process this run")
	api := flag.String("api", "http://127.0.0.1:8002", "DFI backend base URL")
	parallel := flag.Int("parallel", 2, "concurrent claim workers")
	checkOnly := flag.Bool("check", false, "only hit /index/go/status and exit")
	flag.Parse()

	client := &http.Client{Timeout: 10 * time.Minute}
	base := trimSlash(*api)

	if err := heartbeat(client, base); err != nil {
		fmt.Fprintf(os.Stderr, "go-indexer: heartbeat failed: %v\n", err)
		if *checkOnly {
			os.Exit(1)
		}
	}

	st, err := fetchStatus(client, base)
	if err != nil {
		fmt.Fprintf(os.Stderr, "go-indexer: status failed: %v\n", err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "go-indexer: enabled=%v alive=%v last_fps=%.3f\n", st.Enabled, st.Alive, st.LastFilesPerSec)
	if *checkOnly {
		if !st.Enabled {
			fmt.Println("status=toggle_off")
			os.Exit(2)
		}
		fmt.Println("status=ok")
		os.Exit(0)
	}
	if !st.Enabled {
		fmt.Fprintf(os.Stderr, "go-indexer: turn on Settings → Go indexer canary, then retry\n")
		os.Exit(2)
	}

	// Keep heartbeat fresh so Python reserves slots and does not adopt our claims.
	stopHB := make(chan struct{})
	var hbWG sync.WaitGroup
	hbWG.Add(1)
	go func() {
		defer hbWG.Done()
		t := time.NewTicker(20 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-stopHB:
				return
			case <-t.C:
				if err := heartbeat(client, base); err != nil {
					fmt.Fprintf(os.Stderr, "go-indexer: heartbeat: %v\n", err)
				}
			}
		}
	}()
	defer func() {
		close(stopHB)
		hbWG.Wait()
	}()

	start := time.Now()
	var okCount, errCount atomic.Int64
	var downloadBytes atomic.Int64
	var processed atomic.Int64

	workers := max(1, *parallel)
	jobs := make(chan claimItem, workers*2)
	var wg sync.WaitGroup

	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for item := range jobs {
				nbytes, err := processOne(client, base, item)
				if err != nil {
					errCount.Add(1)
					fmt.Fprintf(os.Stderr, "fail %s (%s): %v\n", item.Name, item.ID, err)
					_ = failFile(client, base, item.ID, err.Error())
					continue
				}
				okCount.Add(1)
				downloadBytes.Add(nbytes)
				fmt.Fprintf(os.Stderr, "ok %s bytes=%d\n", item.Name, nbytes)
			}
		}()
	}

	for processed.Load() < int64(*n) {
		remaining := int(*n) - int(processed.Load())
		batch, err := claim(client, base, min(remaining, workers))
		if err != nil {
			fmt.Fprintf(os.Stderr, "claim error: %v\n", err)
			break
		}
		if !batch.Enabled {
			fmt.Fprintf(os.Stderr, "toggle turned off mid-run\n")
			break
		}
		if len(batch.Items) == 0 {
			fmt.Fprintf(os.Stderr, "no more pending images to claim\n")
			break
		}
		for _, item := range batch.Items {
			if processed.Load() >= int64(*n) {
				break
			}
			processed.Add(1)
			jobs <- item
		}
	}
	close(jobs)
	wg.Wait()

	elapsed := time.Since(start)
	_ = report(client, base, int(okCount.Load()), int(errCount.Load()), elapsed.Milliseconds(), downloadBytes.Load())

	fps := float64(okCount.Load()) / maxFloat(elapsed.Seconds(), 0.001)
	fmt.Printf(
		"elapsed_ms=%d files_ok=%d files_err=%d download_bytes=%d files_per_sec=%.3f status=ok\n",
		elapsed.Milliseconds(),
		okCount.Load(),
		errCount.Load(),
		downloadBytes.Load(),
		fps,
	)
}

func processOne(client *http.Client, base string, item claimItem) (int64, error) {
	req, err := http.NewRequest(http.MethodGet, base+"/drive/files/"+item.ID+"/download", nil)
	if err != nil {
		return 0, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return 0, fmt.Errorf("download HTTP %d: %s", resp.StatusCode, string(body))
	}
	n, err := io.Copy(io.Discard, resp.Body)
	if err != nil {
		return n, err
	}

	creq, err := http.NewRequest(http.MethodPost, base+"/index/go/complete/"+item.ID, nil)
	if err != nil {
		return n, err
	}
	cresp, err := client.Do(creq)
	if err != nil {
		return n, err
	}
	defer cresp.Body.Close()
	if cresp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(cresp.Body, 512))
		return n, fmt.Errorf("complete HTTP %d: %s", cresp.StatusCode, string(body))
	}
	return n, nil
}

func claim(client *http.Client, base string, limit int) (*claimResponse, error) {
	url := fmt.Sprintf("%s/index/go/claim?limit=%d", base, max(1, limit))
	req, err := http.NewRequest(http.MethodPost, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("claim HTTP %d: %s", resp.StatusCode, string(body))
	}
	var out claimResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return &out, nil
}

func failFile(client *http.Client, base, id, detail string) error {
	url := fmt.Sprintf("%s/index/go/fail/%s?detail=%s", base, id, urlQuery(detail))
	req, err := http.NewRequest(http.MethodPost, url, nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return nil
}

func heartbeat(client *http.Client, base string) error {
	req, err := http.NewRequest(http.MethodPost, base+"/index/go/heartbeat", nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusForbidden {
		return fmt.Errorf("toggle off (403)")
	}
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}
	return nil
}

func report(client *http.Client, base string, ok, errn int, elapsedMs int64, nbytes int64) error {
	payload, _ := json.Marshal(map[string]int64{
		"files_ok":       int64(ok),
		"files_err":      int64(errn),
		"elapsed_ms":     elapsedMs,
		"download_bytes": nbytes,
	})
	req, err := http.NewRequest(http.MethodPost, base+"/index/go/report", bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return nil
}

func fetchStatus(client *http.Client, base string) (*goStatus, error) {
	resp, err := client.Get(base + "/index/go/status")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}
	var st goStatus
	if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
		return nil, err
	}
	return &st, nil
}

func trimSlash(s string) string {
	for len(s) > 0 && s[len(s)-1] == '/' {
		s = s[:len(s)-1]
	}
	return s
}

func urlQuery(s string) string {
	b := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' {
			b = append(b, c)
		} else if c == ' ' {
			b = append(b, '+')
		} else {
			b = append(b, '%')
			const hex = "0123456789ABCDEF"
			b = append(b, hex[c>>4], hex[c&15])
		}
	}
	if len(b) > 200 {
		b = b[:200]
	}
	return string(b)
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func maxFloat(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
