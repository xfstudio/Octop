import { lazy, Suspense, useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Boxes, Globe, TerminalSquare } from "lucide-react";
import PageLoading from "../../../components/PageLoading";
import PageShell from "../../../layouts/PageShell";
import { usePathTabs } from "../../../hooks/usePathTabs";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import { userCan } from "../../../utils/permissions";
import TerminalPage from "../Terminal";
import RemoteBrowserPage from "../RemoteBrowser";
import styles from "./index.module.less";

const PlatformPage = lazy(() => import("../../Longterm"));

export type WorkbenchTab = "platform" | "terminal" | "browser";

const WORKBENCH_TABS = [
  "platform",
  "browser",
  "terminal",
] as const satisfies readonly WorkbenchTab[];

const TAB_ICONS = {
  platform: Boxes,
  browser: Globe,
  terminal: TerminalSquare,
} as const;

interface WorkbenchPageProps {
  /** True when the workbench keep-alive surface is currently shown. */
  isVisible?: boolean;
}

export default function WorkbenchPage({
  isVisible = true,
}: WorkbenchPageProps) {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const isAllowed = useCallback(
    (tab: WorkbenchTab) => tab === "platform" || userCan(user, tab),
    [user],
  );
  const defaultTab: WorkbenchTab = "platform";

  const { activeTab, handleTabChange, isMounted } = usePathTabs({
    basePath: "/workbench",
    tabs: WORKBENCH_TABS,
    storageKey: "octop:workbench:tab",
    defaultTab,
    isAllowed,
  });

  const pathTabs = useMemo(
    () => ({
      value: activeTab,
      onChange: handleTabChange,
      options: WORKBENCH_TABS.filter((value) => isAllowed(value)).map(
        (value) => {
          const Icon = TAB_ICONS[value];
          return {
            value,
            label: t(`workbench.tabs.${value}`),
            icon: <Icon size={14} strokeWidth={2} />,
          };
        },
      ),
    }),
    [activeTab, handleTabChange, isAllowed, t],
  );

  const browserVisible = isVisible && activeTab === "browser";
  const terminalVisible = isVisible && activeTab === "terminal";
  const pageTitle = `${t("workbench.title")} / ${t(
    `workbench.tabs.${activeTab}`,
  )}`;

  return (
    <PageShell
      title={pageTitle}
      subtitle={t("workbench.description")}
      fill
      pathTabs={pathTabs}
    >
      <div className={styles.panels}>
        {isMounted("platform") && (
          <div
            className={styles.panel}
            style={{ display: activeTab === "platform" ? "flex" : "none" }}
            aria-hidden={activeTab !== "platform"}
          >
            <Suspense fallback={<PageLoading />}>
              <PlatformPage
                key={user?.id ?? "offline"}
                embedded
                isVisible={isVisible && activeTab === "platform"}
              />
            </Suspense>
          </div>
        )}
        {isMounted("browser") && (
          <div
            className={styles.panel}
            style={{ display: activeTab === "browser" ? "flex" : "none" }}
            aria-hidden={activeTab !== "browser"}
          >
            <RemoteBrowserPage embedded isVisible={browserVisible} />
          </div>
        )}
        {isMounted("terminal") && (
          <div
            className={styles.panel}
            style={{ display: activeTab === "terminal" ? "flex" : "none" }}
            aria-hidden={activeTab !== "terminal"}
          >
            <TerminalPage embedded isVisible={terminalVisible} />
          </div>
        )}
      </div>
    </PageShell>
  );
}
