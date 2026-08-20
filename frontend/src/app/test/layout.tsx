import { TestShell } from "@/components/test-shell";

export default function TestLayout({ children }: { children: React.ReactNode }) {
  return <TestShell>{children}</TestShell>;
}
