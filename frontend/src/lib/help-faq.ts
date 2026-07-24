export type FaqItem = {
  id: string;
  question: string;
  answer: string;
  href?: string;
};

export type FaqCategory = {
  id: string;
  title: string;
  blurb: string;
  items: FaqItem[];
};

export const FAQ_CATEGORIES: FaqCategory[] = [
  {
    id: "getting-started",
    title: "Getting started",
    blurb: "Sign in and understand what DriveFaceIndexer does.",
    items: [
      {
        id: "what-is-dfi",
        question: "What does DriveFaceIndexer do?",
        answer:
          "DriveFaceIndexer indexes Google Drive folders (and optional YouTube videos), detects faces with InsightFace, embeds media with Gemini, and lets you search by person, visual content, or captions. Name someone once in Review Queue and future appearances can auto-tag.",
      },
      {
        id: "sign-in",
        question: "How do I sign in?",
        answer:
          "Open the app and sign in with your Masters' Union Google account (@mastersunion.org). Sign-in is remembered for about 90 days. Use Logout in the sidebar footer when you need to switch accounts.",
      },
      {
        id: "first-run",
        question: "What should I do on first use?",
        answer:
          "1) Open Folders and connect Google Drive. 2) Pick the folder to index. 3) Click Start Index (or enable auto-index in Settings). 4) Open Review Queue and name unknown faces. 5) Use Search, Video Carousel, or People once indexing has processed files.",
        href: "/folders",
      },
    ],
  },
  {
    id: "drive",
    title: "Connect Drive & folders",
    blurb: "OAuth, folder picker, context notes, and indexing controls.",
    items: [
      {
        id: "connect-drive",
        question: "How do I connect Google Drive?",
        answer:
          "Go to Folders. If Drive is not connected, click Connect Google Drive and complete Google OAuth. When you return with a successful connection, Folders reloads your session and selected folder.",
        href: "/folders",
      },
      {
        id: "pick-folder",
        question: "How do I choose which folder to index?",
        answer:
          "On Folders, use the folder picker (Choose folder / change folder). You can pick folders from My Drive or Shared drives. After selecting, run Start Index or wait for auto-index if it is enabled.",
        href: "/folders",
      },
      {
        id: "start-index",
        question: "How do I start indexing?",
        answer:
          "On Folders, click Start Index. The button shows Indexing… while a run is active. Progress appears in the status summary and the indexing queue. Dashboard also shows counts by status (processed, pending, processing, error, skipped).",
        href: "/folders",
      },
      {
        id: "folder-context",
        question: "What are folder descriptions (context)?",
        answer:
          "On Folders, add a short description to a connected folder path. That text is embedded and improves search relevance for files under that folder. Use Add context or Edit next to a folder row.",
        href: "/folders",
      },
      {
        id: "index-queue",
        question: "How do I view the indexing queue?",
        answer:
          "On Folders, open the indexing queue to browse files by status (All, Pending, Active, Completed, Failed, Skipped). From Failed items you can Retry a single file. Use filters and paging to find stuck items.",
        href: "/folders",
      },
    ],
  },
  {
    id: "youtube",
    title: "YouTube",
    blurb: "Register YouTube URLs into the same index pipeline.",
    items: [
      {
        id: "youtube-add",
        question: "How do I index YouTube videos?",
        answer:
          "On Folders, find YouTube videos. Paste one or more YouTube URLs or video IDs, then submit. Missing videos are downloaded (yt-dlp) into the shared download location and queued for indexing. Use Start Index or auto-sync if they stay pending.",
        href: "/folders",
      },
      {
        id: "youtube-status",
        question: "YouTube register succeeded but I see nothing in search yet — why?",
        answer:
          "Registration only queues downloads/index work. Wait until the file shows as processed in the queue or Dashboard. Large videos take longer. Check Failed in the queue and Retry if needed.",
        href: "/folders",
      },
    ],
  },
  {
    id: "search",
    title: "Search",
    blurb: "Text, person, folder, captions, and re-rank options.",
    items: [
      {
        id: "search-basic",
        question: "How do I search indexed media?",
        answer:
          "Open Search, type a query, and press Search. Optionally filter by person, media type (all / image / video), and folder path. Results show matching files and timestamped moments for videos.",
        href: "/search",
      },
      {
        id: "search-captions-rerank",
        question: "What do Captions and Re-rank do?",
        answer:
          "Captions includes caption/transcript-style matching when available. Re-rank reorders video moments for better relevance (slower). Defaults for both can be set under Settings → Search, and overridden per query on the Search page.",
        href: "/search",
      },
      {
        id: "search-preview",
        question: "How do I preview or open a result?",
        answer:
          "Click a result to preview. Videos can seek to the matched timestamp. Use Drive / download actions on a result when available. Person tags link into the People section when faces were recognized.",
        href: "/search",
      },
    ],
  },
  {
    id: "carousel",
    title: "Video Carousel",
    blurb: "Browse search hits one moment at a time.",
    items: [
      {
        id: "carousel-use",
        question: "How do I use Video Carousel?",
        answer:
          "Open Video Carousel under Find. Search for moments, multi-select frames (or Select all from this video), draft a script in Script studio, then click Generate carousel from this video. Review the ordered slides (hook + timestamp + caption). You can still Use snapshot to attach one frame to a script draft.",
        href: "/search/carousel",
      },
    ],
  },
  {
    id: "reverse-face",
    title: "Reverse Face",
    blurb: "Match an uploaded face against your indexed people.",
    items: [
      {
        id: "reverse-upload",
        question: "How do I reverse-search a face photo?",
        answer:
          "Open Reverse Face (Find). Drag-and-drop or upload a clear face photo, then run search. The largest detected face is matched against your people library. Open Profile on a match to jump to that person.",
        href: "/labs/reverse-face",
      },
      {
        id: "reverse-crawl",
        question: "How do I reverse-search from image URLs?",
        answer:
          "On Reverse Face, paste public image URLs (one per line or comma-separated) and run crawl. The lab downloads those images and matches detected faces the same way as an upload.",
        href: "/labs/reverse-face",
      },
      {
        id: "reverse-leadership",
        question: "What does Scan Executive Leaders do?",
        answer:
          "On Reverse Face, Scan Executive Leaders scrapes the Masters' Union about-us Executive Leaders portraits and reverse-matches them against your indexed people. Optionally enable web reverse-search (slower) for extra confirmation on top matches.",
        href: "/labs/reverse-face",
      },
    ],
  },
  {
    id: "people-review",
    title: "People & Review Queue",
    blurb: "Name faces, merge identities, and manage people.",
    items: [
      {
        id: "review-queue",
        question: "How do I name unknown faces?",
        answer:
          "Open Review Queue. For each cluster, type a name and confirm, or merge into an existing person, or ignore forever. After naming, future similar faces can auto-attach to that person during indexing.",
        href: "/review",
      },
      {
        id: "people-manage",
        question: "How do I rename, role-tag, or delete a person?",
        answer:
          "Open People. Edit the name on a card, set a role with the role selector, or delete a person (confirm first). Open a person for a fuller profile and face gallery when available.",
        href: "/people",
      },
    ],
  },
  {
    id: "library",
    title: "Library",
    blurb: "Browse indexed files by folder and status.",
    items: [
      {
        id: "library-browse",
        question: "How do I browse the library?",
        answer:
          "Open Library to walk the folder tree of tracked Drive files. Filter by status (processed, pending, error, skipped, missing caption/embed, etc.). Open a file for preview, Drive link, or download. Pause/resume folder indexing where controls are shown.",
        href: "/library",
      },
      {
        id: "manual-face-tag",
        question: "How do I manually tag faces on an image?",
        answer:
          "Enable Settings → Experimental → Manual face tagging. Then open an image in Library; face boxes appear so you can name faces without re-indexing or Gemini uploads.",
        href: "/settings",
      },
    ],
  },
  {
    id: "settings",
    title: "Settings",
    blurb: "Search defaults, auto-index, and retries.",
    items: [
      {
        id: "settings-search",
        question: "What do the Search toggles mean?",
        answer:
          "Gemini File Search at query time: optional extra semantic pass (slower; off by default). Parallel query variants: faster but can reduce quality. Use captions in search / Video re-rank default: synced defaults for the Search page toggles.",
        href: "/settings",
      },
      {
        id: "settings-auto-index",
        question: "How does auto indexing work?",
        answer:
          "Turn on Automatically sync Drive and upload new or changed files. Drive Connector can webhook on changes; the fallback poll interval (seconds) is a backup. Save interval after changing the number.",
        href: "/settings",
      },
      {
        id: "settings-retries",
        question: "What do Retry errored / skipped files do?",
        answer:
          "Retry errored files re-queues failed items on auto-index and manual runs. Retry skipped files re-queues most skips (not folder-paused or unsupported types). Follow folder shortcuts includes shortcut targets when syncing. Go indexer canary is experimental parallel image claiming.",
        href: "/settings",
      },
    ],
  },
  {
    id: "errors",
    title: "Errors & troubleshooting",
    blurb: "Recover from failed index jobs and connection issues.",
    items: [
      {
        id: "backend-down",
        question: "I see a backend disconnected / service unavailable message",
        answer:
          "The frontend cannot reach the API. Confirm the backend is running (default http://localhost:8000) and that Next.js points at the correct API base. Use Retry on the error card after the service is back.",
      },
      {
        id: "index-errors",
        question: "How do I fix files stuck in error?",
        answer:
          "On Folders, open Failed in the queue or the index errors list and click Retry on the file. Or enable Settings → Retry errored files and run Start Index / wait for auto-index. Check the error message for Drive permission, download, or unsupported-type issues.",
        href: "/folders",
      },
      {
        id: "drive-oauth-fail",
        question: "Drive connection failed after Google sign-in",
        answer:
          "Folders shows the OAuth error query when redirect fails. Try Connect Google Drive again. Confirm Drive Connector is running and the backend has a valid DRIVE_CONNECTOR_API_KEY. Sign out of the app only if you need a different Google identity for the UI gate — Drive OAuth is separate.",
        href: "/folders",
      },
      {
        id: "empty-search",
        question: "Search returns nothing",
        answer:
          "Confirm Dashboard/Folders show processed files. Narrow filters (person, mime, folder) may hide hits — try All types and clear person/folder. Disable overly strict toggles or try Video Carousel with a simpler query. New files need to finish indexing first.",
        href: "/search",
      },
      {
        id: "contact-support",
        question: "How do I contact support?",
        answer:
          "Use Contact Support in the sidebar footer. It opens your email client to the configured support address with a DriveFaceIndexer subject line.",
      },
    ],
  },
];
