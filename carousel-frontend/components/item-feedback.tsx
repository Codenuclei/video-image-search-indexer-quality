"use client";

import { useEffect, useRef, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { apiClient, formatApiError, type CarouselItemFeedback } from "@/lib/api";
import { cn } from "@/lib/utils";

type Props = {
  driveFileId: string;
  kind: "theme" | "hook";
  targetKey: string;
  targetLabel: string;
  initial?: CarouselItemFeedback | null;
  onSaved?: (item: CarouselItemFeedback) => void;
  className?: string;
};

export function ItemFeedback({
  driveFileId,
  kind,
  targetKey,
  targetLabel,
  initial,
  onSaved,
  className,
}: Props) {
  const [rating, setRating] = useState<"up" | "down" | null>(
    (initial?.rating as "up" | "down" | null | undefined) ?? null
  );
  const [comment, setComment] = useState(initial?.comment ?? "");
  const [open, setOpen] = useState(Boolean(initial?.comment));
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setRating((initial?.rating as "up" | "down" | null | undefined) ?? null);
    setComment(initial?.comment ?? "");
    if (initial?.comment) setOpen(true);
  }, [initial?.id, initial?.rating, initial?.comment, targetKey]);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  async function persist(nextRating: "up" | "down" | null, nextComment: string) {
    if (!driveFileId || !targetKey) return;
    setSaving(true);
    setNote(null);
    try {
      const res = await apiClient.carouselFeedbackUpsert({
        drive_file_id: driveFileId,
        target_kind: kind,
        target_key: targetKey,
        target_label: targetLabel,
        rating: nextRating,
        comment: nextComment,
      });
      onSaved?.(res.item);
      setNote("Saved");
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setNote(null), 1200);
    } catch (e) {
      setNote(formatApiError(e, "Could not save feedback"));
    } finally {
      setSaving(false);
    }
  }

  function onThumb(next: "up" | "down") {
    const value = rating === next ? null : next;
    setRating(value);
    void persist(value, comment);
  }

  function onCommentBlur() {
    const trimmed = comment.trim();
    if (trimmed === (initial?.comment ?? "").trim() && rating === (initial?.rating ?? null)) {
      return;
    }
    void persist(rating, trimmed);
  }

  return (
    <div
      className={cn("item-feedback", className)}
      onClick={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
      data-testid={`item-feedback-${kind}`}
    >
      <div className="item-feedback-row">
        <button
          type="button"
          className={cn("item-feedback-thumb", rating === "up" && "is-on is-up")}
          aria-pressed={rating === "up"}
          aria-label="Thumbs up"
          title="Helpful"
          disabled={saving}
          onClick={() => onThumb("up")}
        >
          <ThumbsUp size={12} strokeWidth={2.25} />
        </button>
        <button
          type="button"
          className={cn("item-feedback-thumb", rating === "down" && "is-on is-down")}
          aria-pressed={rating === "down"}
          aria-label="Thumbs down"
          title="Not helpful"
          disabled={saving}
          onClick={() => onThumb("down")}
        >
          <ThumbsDown size={12} strokeWidth={2.25} />
        </button>
        <button
          type="button"
          className="item-feedback-comment-toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "Hide note" : comment.trim() ? "Edit note" : "Add note"}
        </button>
        {note ? <span className="item-feedback-note">{note}</span> : null}
      </div>
      {open ? (
        <textarea
          className="item-feedback-textarea"
          rows={2}
          value={comment}
          placeholder={`Short note on this ${kind}…`}
          disabled={saving}
          onChange={(e) => setComment(e.target.value)}
          onBlur={onCommentBlur}
          maxLength={800}
        />
      ) : null}
    </div>
  );
}
