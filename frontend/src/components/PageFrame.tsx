import type React from "react";
import { cn } from "@/lib/utils";

export type PageFrameWidth = "3xl" | "4xl" | "5xl" | "6xl";

const widthClasses: Record<PageFrameWidth, string> = {
  "3xl": "max-w-3xl",
  "4xl": "max-w-4xl",
  "5xl": "max-w-5xl",
  "6xl": "max-w-6xl",
};

export interface PageFrameProps extends React.HTMLAttributes<HTMLDivElement> {
  width?: PageFrameWidth;
}

export function PageFrame({ width = "4xl", className, children, ...props }: PageFrameProps) {
  return (
    <div className={cn("mx-auto w-full p-6", widthClasses[width], className)} {...props}>
      {children}
    </div>
  );
}
