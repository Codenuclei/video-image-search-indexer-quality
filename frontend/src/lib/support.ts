export const SUPPORT_EMAIL =
  process.env.NEXT_PUBLIC_SUPPORT_EMAIL ?? "abhishek.ghosh1@mastersunion.org";

export const supportMailto = (subject = "DriveFaceIndexer Support") =>
  `mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}`;
