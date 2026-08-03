export default function CarouselLogo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-hidden="true"
    >
      <rect x="2" y="6" width="16" height="20" rx="2.5" fill="currentColor" opacity="0.35" />
      <rect x="8" y="4" width="16" height="20" rx="2.5" fill="currentColor" opacity="0.55" />
      <rect x="14" y="2" width="16" height="20" rx="2.5" fill="currentColor" />
    </svg>
  );
}
