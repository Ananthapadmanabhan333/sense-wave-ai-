import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SenseWave AI",
  description: "Camera-free WiFi sensing, honestly reported.",
};

/**
 * The vitals disclaimer is rendered here, in the root layout, rather than per
 * page. Product rule 6 requires it to be persistent and non-dismissible, and
 * putting it at the root means no view can ship without it.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <div className="flex min-h-screen flex-col">
          <main className="flex-1">{children}</main>
          <footer
            role="note"
            className="border-t border-amber-900/50 bg-amber-950/40 px-4 py-3 text-xs text-amber-200"
          >
            Breathing and heart rate are contactless RF estimates with no clinical
            validation. SenseWave is not a medical device and must not be used for
            diagnosis or treatment.
          </footer>
        </div>
      </body>
    </html>
  );
}
