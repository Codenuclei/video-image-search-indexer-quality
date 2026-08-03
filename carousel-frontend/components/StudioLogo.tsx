export default function StudioLogo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-hidden="true"
    >
      <rect x="2" y="6" width="18" height="20" rx="2.5" fill="currentColor" opacity="0.28" />
      <rect x="7" y="4" width="18" height="20" rx="2.5" fill="currentColor" opacity="0.55" />
      <rect x="12" y="2" width="18" height="20" rx="2.5" fill="currentColor" />
    </svg>
  );
}
