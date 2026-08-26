"use client";

import { CircleHelp } from "lucide-react";
import { HelpFaqPanel } from "@/components/help-faq-panel";
import { TEST_FAQ_CATEGORIES } from "@/lib/help-faq";
import { useRegisterTestShellChrome } from "@/lib/test-shell-chrome";

export default function TestHelpPage() {
  useRegisterTestShellChrome(
    <h1 className="flex items-center gap-2 text-lg font-semibold tracking-tight sm:text-xl">
      <CircleHelp size={20} className="text-blue-600 dark:text-blue-400" aria-hidden />
      How to / FAQ
    </h1>,
    []
  );

  return <HelpFaqPanel accent="blue" showTitle={false} categories={TEST_FAQ_CATEGORIES} />;
}
