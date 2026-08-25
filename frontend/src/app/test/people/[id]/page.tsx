"use client";

import PersonDetailPage from "@/app/people/[id]/page";

/** Same person detail as /people/[id], but stays inside the test shell. */
export default function TestPersonDetailPage() {
  return <PersonDetailPage />;
}
