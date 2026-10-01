/** Root layout shared by every page in the MediBot interface. */
import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = { title: "MediBot", description: "MediAssist internal knowledge assistant" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  /** Render the application shell. */
  return <html lang="en"><body>{children}</body></html>;
}

