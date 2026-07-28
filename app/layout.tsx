import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(
    process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000",
  ),
  title: "智耕凭证农场",
  description: "中移互联网智能体身份凭证准入与行为追溯 Demo",
  openGraph: {
    title: "智耕凭证农场",
    description: "身份可信 · 行为可溯",
    images: [{ url: "/og.png", width: 1677, height: 943 }],
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "智耕凭证农场",
    description: "身份可信 · 行为可溯",
    images: ["/og.png"],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
