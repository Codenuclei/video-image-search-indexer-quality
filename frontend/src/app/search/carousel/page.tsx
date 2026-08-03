import { redirect } from "next/navigation";

/**
 * Video Carousel is soft-disabled on the main DriveFaceIndexer app.
 * The studio lives on the separate dfi-carousel service (carousel-frontend/).
 * Studio implementation kept off-route in carousel-studio-page.tsx for restore.
 */
export default function CarouselSearchPageDisabled() {
  redirect("/search");
}
