import { Inbox } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { InboxPage } from "./InboxPage";
import { useInboxBadge } from "./usePendingApprovals";

export const route: PageRoute = {
  path: "/projects/:projectId/inbox",
  element: <InboxPage />,
  nav: {
    area: "project",
    label: "Inbox",
    order: 10,
    icon: Inbox,
    badge: useInboxBadge,
  },
};
