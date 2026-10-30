import type { PageRoute } from "@/app/routes";
import { OnboardingStepPage } from "./OnboardingStepPage";
import { ONBOARDING_PATH } from "./paths";

// No nav entry: the step is reached from the add-project wizard, not from the sidebar.
export const route: PageRoute = {
  path: ONBOARDING_PATH,
  element: <OnboardingStepPage />,
};
