import StudioLogo from "./StudioLogo";

const LINKS = [
  { label: "Studio", href: "/carousel" },
  { label: "How it works", href: "#how-it-works" },
] as const;

export default function Navbar() {
  return (
    <nav className="fixed top-0 left-0 right-0 z-50 px-6 sm:px-10 md:px-14 py-4 sm:py-5">
      <div className="flex items-center justify-between">
        <a href="/" className="flex items-center gap-2.5 text-[#191919]">
          <StudioLogo className="h-6 w-6 text-[#191919]" />
          <span className="font-semibold text-base tracking-tight text-[#191919]">
            Carousel Studio
          </span>
        </a>

        <div className="hidden md:flex items-center gap-8">
          {LINKS.map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="text-sm text-[#191919]/70 hover:text-[#191919] transition-colors duration-200"
            >
              {link.label}
            </a>
          ))}
        </div>

        <a
          href="/carousel"
          className="px-5 py-2.5 bg-[#191919] text-white text-sm font-medium rounded-lg hover:bg-[#191919]/90 transition-colors duration-200"
        >
          Open studio
        </a>
      </div>
    </nav>
  );
}
