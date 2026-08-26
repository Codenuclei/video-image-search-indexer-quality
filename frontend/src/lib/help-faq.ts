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
          "1) Open Folders and connect Google Drive. 2) Pick the folder to index. 3) Open Admin and click Start Index (or enable auto-index in Settings). 4) Open Review Queue and name unknown faces. 5) Use Search or People once indexing has processed files.",
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
          "On Folders, use the folder picker (Choose folder / change folder). You can pick folders from My Drive or Shared drives. After selecting, open Admin → Start Index or wait for auto-index if it is enabled.",
        href: "/folders",
      },
      {
        id: "start-index",
        question: "How do I start indexing?",
        answer:
          "Open Admin and click Start Index. Files whose content hash is already indexed are skipped. Progress appears in the sidebar banner and Dashboard. Use Backfill missing only for incomplete rows.",
        href: "/admin",
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
          "On Folders, open the indexing queue to browse files by status. From Failed items you can Retry a single file. Bulk skip-reason retries are on Admin.",
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
          "YouTube videos can be registered into the shared library and indexed like Drive media (transcript, frames, visual search). Paste YouTube URLs or video IDs when the YouTube register control is available, then run Start Index from Admin (or wait for auto-sync) if they stay pending.",
        href: "/help",
      },
      {
        id: "youtube-status",
        question: "YouTube register succeeded but I see nothing in search yet — why?",
        answer:
          "Registration only queues downloads/index work. Wait until the file shows as processed in the queue or Dashboard. Large videos take longer. Check Failed in the Library or queue and Retry if needed.",
        href: "/library",
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
  // Video Carousel FAQ removed — studio disabled on main app (dfi-carousel).
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
          "Confirm Dashboard/Folders show processed files. Narrow filters (person, mime, folder) may hide hits — try All types and clear person/folder. Disable overly strict toggles or try a simpler query. New files need to finish indexing first.",
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

/** FAQ copy for the TestShell UI (/test/*). Hrefs point at test routes directly. */
export const TEST_FAQ_CATEGORIES: FaqCategory[] = [
  {
    id: "getting-started",
    title: "Getting started",
    blurb: "Sign in and find your way around this UI.",
    items: [
      {
        id: "what-is-app",
        question: "What can I do here?",
        answer:
          "Search indexed Drive photos and videos, browse Indexed Folders, manage people (indexed, MU leaders, and un-indexed faces), and reverse-search a face from an uploaded photo. Use How to / FAQ anytime from the sidebar.",
      },
      {
        id: "sign-in",
        question: "How do I sign in?",
        answer:
          "Sign in with your Masters' Union Google account (@mastersunion.org). Your session lasts about 90 days. Open the account menu (avatar) in the top-right to sign out or disconnect Drive.",
      },
      {
        id: "first-run",
        question: "What should I do on first use?",
        answer:
          "1) Open Indexed Folders and connect Google Drive if prompted. 2) Pick the Drive folder to index (use Current UI → Admin → Start Index if indexing has not run yet). 3) Open People Directory → Un-Indexed People to name unknown faces. 4) Use Search once files are processed.",
        href: "/test/folders",
      },
      {
        id: "nav-overview",
        question: "Where is everything in the sidebar?",
        answer:
          "Search is the home view. Library has Indexed Folders and People Directory. How to / FAQ and Settings are in the footer. Current UI opens the classic app (Dashboard, Admin, Review Queue) when you need ops tools that are not in this shell.",
        href: "/test/search",
      },
    ],
  },
  {
    id: "search",
    title: "Search",
    blurb: "Header search, filters, captions, and results.",
    items: [
      {
        id: "search-basic",
        question: "How do I search?",
        answer:
          "On Search, type in the header bar and press the blue search button (or Enter). Use the icon filters for type (images/PDFs), folder, and person. Results appear below as files and video moments.",
        href: "/test/search",
      },
      {
        id: "search-person-name",
        question: "How do I find photos of a named person?",
        answer:
          "Type their name in the search box (case and spacing are flexible), or pick them from the person filter. A pure person-name query returns their face-tagged roster; adding an action like “cooking” still uses captions when helpful.",
        href: "/test/search",
      },
      {
        id: "search-captions",
        question: "What does the Captions toggle do?",
        answer:
          "The document icon in the search bar turns caption matching on or off. Leave it on for scene/action queries. For a pure person name, the roster is shown either way so captions do not shrink tagged results.",
        href: "/test/search",
      },
      {
        id: "search-preview",
        question: "How do I preview a result?",
        answer:
          "Click a result card to enlarge or open it. Video moments can seek to the matched time. Person chips and +N face matches link into People Directory profiles when available.",
        href: "/test/search",
      },
    ],
  },
  {
    id: "reverse-face",
    title: "Search by image",
    blurb: "Match an uploaded face against indexed people.",
    items: [
      {
        id: "reverse-upload",
        question: "How do I reverse-search a face photo?",
        answer:
          "On Search, click the image-plus icon in the header, upload a clear face photo, and wait for matches. Results show similar people with scores and file counts. Open a match to go to that person’s profile. Use the X control to clear the image search.",
        href: "/test/search#reverse-face",
      },
      {
        id: "reverse-mu",
        question: "Where is MU / leadership face matching?",
        answer:
          "Open People Directory → MU People. That tab runs leadership / reverse-face tools against your indexed people library.",
        href: "/test/people?tab=mu",
      },
    ],
  },
  {
    id: "folders",
    title: "Indexed Folders",
    blurb: "Browse the library tree and Drive session.",
    items: [
      {
        id: "folders-browse",
        question: "How do I browse indexed folders?",
        answer:
          "Open Indexed Folders. Use the folder tree and file grid to walk your library. The header shows Drive email / selected folder and total file count. Refresh reloads the tree.",
        href: "/test/folders",
      },
      {
        id: "connect-drive",
        question: "How do I connect or change Google Drive?",
        answer:
          "On Indexed Folders, use the Drive session control in the header to connect, pick a folder, or switch. You can also disconnect Drive from the account menu (avatar) in the top-right.",
        href: "/test/folders",
      },
      {
        id: "start-index",
        question: "How do I start or check indexing?",
        answer:
          "Indexing controls (Start Index, skip reasons, status chart) live in Current UI → Dashboard and Admin. After indexing runs, return here to Search and Indexed Folders — processed counts show on the classic Dashboard chart.",
        href: "/",
      },
    ],
  },
  {
    id: "people",
    title: "People Directory",
    blurb: "Indexed people, MU people, and naming new faces.",
    items: [
      {
        id: "people-indexed",
        question: "How do I browse named people?",
        answer:
          "Open People Directory → Indexed People. Open a card for that person’s profile, roles, and media. Rename or set roles from the person view.",
        href: "/test/people",
      },
      {
        id: "people-unindexed",
        question: "How do I name unknown faces?",
        answer:
          "Open People Directory → Un-Indexed People. That tab is the review queue: name a cluster, merge into an existing person, or ignore. Future similar faces can auto-attach after naming.",
        href: "/test/people?tab=unindexed",
      },
      {
        id: "people-profile",
        question: "How do I open someone’s profile from search?",
        answer:
          "From Search results or reverse-face matches, follow the person / +N chip into their profile under People Directory.",
        href: "/test/people",
      },
    ],
  },
  {
    id: "settings",
    title: "Settings",
    blurb: "Defaults that apply across search and indexing.",
    items: [
      {
        id: "settings-open",
        question: "Where are Settings?",
        answer:
          "Sidebar footer → Settings (same settings page as the classic app). Change search defaults (captions, re-rank), auto-index, and retries there. Return via Search or the browser back button.",
        href: "/settings",
      },
      {
        id: "settings-captions",
        question: "How do I change the default Captions toggle?",
        answer:
          "In Settings, set “Use captions in search”. The Search header toggle still overrides per session.",
        href: "/settings",
      },
    ],
  },
  {
    id: "errors",
    title: "Troubleshooting",
    blurb: "Empty results, Drive issues, and when to use Current UI.",
    items: [
      {
        id: "empty-search",
        question: "Search returns nothing",
        answer:
          "Clear person / folder / type filters and try again. Confirm Indexed Folders shows files and that indexing has finished (Current UI → Dashboard). New uploads need to finish processing first. For a person name, check spelling against People Directory.",
        href: "/test/search",
      },
      {
        id: "drive-oauth-fail",
        question: "Drive will not connect",
        answer:
          "Retry Connect from Indexed Folders. Confirm you use the right Google account. Disconnect Drive from the account menu, then connect again. If it still fails, use Current UI → Folders for the classic OAuth flow.",
        href: "/test/folders",
      },
      {
        id: "need-admin",
        question: "Where do I retry failed files or see skip reasons?",
        answer:
          "Open Current UI → Dashboard for the status chart, skip reasons, and conflicts. Admin has Start Index, count cards, and bulk retries.",
        href: "/",
      },
    ],
  },
];
