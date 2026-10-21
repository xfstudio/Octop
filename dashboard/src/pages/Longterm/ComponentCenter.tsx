import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Select, Space, Tag, Typography } from "antd";
import { RefreshCw, Settings2, Unplug } from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { APP_LOGO_SRC } from "../../assets/brand";
import {
  componentManagementApi,
  type ComponentCatalog,
  type ComponentConnectionCheck,
  type ManagedComponentId,
  type ProjectComponentId,
} from "../../api/modules/componentManagement";
import type { LongtermTask, TaskOperation } from "../../api/modules/longterm";
import ConfigurationDialog from "./ComponentConfigurationDialog";
import {
  COMPONENTS,
  isComponentPermissionError as isPermissionError,
} from "./componentMetadata";
import ComponentProjectDialog from "./ComponentProjectDialog";
import type { OperationSessionSeed } from "./OperationSessionDialog";
import styles from "./ComponentCenter.module.less";

export type ComponentCenterTab =
  | "workflow"
  | "sync"
  | "tasks"
  | "integrations"
  | "evaluation";

export interface ComponentCenterProps {
  connected: boolean;
  isAdmin: boolean;
  actorId?: string | number;
  tenantId: string;
  spaceId: string;
  tenantRole?: string;
  tasks: LongtermTask[];
  canAccessPath: (path: string) => boolean;
  onNavigate: (tab: ComponentCenterTab) => void;
  onOperation: (kind: TaskOperation, task: LongtermTask) => void;
  onChanged?: () => void;
  onSession?: (seed: OperationSessionSeed) => void;
}

const OCTOP_LINKS = [
  ["agents", "/experts"],
  ["models", "/admin/models"],
  ["skills", "/personalization/skills"],
  ["subagents", "/personalization/subagents"],
  ["connectors", "/connectors"],
  ["storage", "/admin/backend"],
  ["users", "/admin/users"],
  ["security", "/admin/security"],
] as const;

const TASK_OPERATIONS: Partial<Record<ManagedComponentId, TaskOperation>> = {
  semantica: "provenance",
  commerceagentbench: "evaluate",
  rna: "evolution",
  hindsight: "memory",
};

export default function ComponentCenter({
  connected,
  isAdmin,
  actorId,
  tenantId,
  spaceId,
  tenantRole,
  tasks,
  canAccessPath,
  onNavigate,
  onOperation,
  onChanged,
  onSession,
}: ComponentCenterProps) {
  const { t } = useTranslation();
  const scope = JSON.stringify([
    connected,
    isAdmin,
    actorId,
    tenantId,
    spaceId,
    tenantRole,
  ]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const [refresh, setRefresh] = useState(0);
  const [snapshot, setSnapshot] = useState<{
    scope: string;
    catalog?: ComponentCatalog;
    error?: string;
    loading: boolean;
  }>({ scope, loading: connected });
  const [dialog, setDialog] = useState<{
    scope: string;
    id: ManagedComponentId;
  } | null>(null);
  const [projectDialog, setProjectDialog] = useState<{
    scope: string;
    id: ProjectComponentId;
  } | null>(null);
  const [selection, setSelection] = useState({ scope, id: "" });
  const [checks, setChecks] = useState<{
    scope: string;
    values: Partial<Record<ManagedComponentId, ComponentConnectionCheck>>;
  }>({ scope, values: {} });
  const [checking, setChecking] = useState<{
    scope: string;
    ids: Set<ManagedComponentId>;
  }>({ scope, ids: new Set() });
  const [notice, setNotice] = useState<{ scope: string; key: string } | null>(
    null,
  );
  const [deniedScope, setDeniedScope] = useState("");
  const checkControllers = useRef(new Set<AbortController>());

  useEffect(() => {
    const controller = new AbortController();
    const requests = checkControllers.current;
    setSnapshot({ scope, loading: connected });
    setChecks({ scope, values: {} });
    setChecking({ scope, ids: new Set() });
    setDeniedScope("");
    if (connected) {
      void componentManagementApi
        .catalog(controller.signal)
        .then((catalog) => {
          if (scopeRef.current !== scope || controller.signal.aborted) return;
          setSnapshot({ scope, loading: false, catalog });
        })
        .catch((cause: unknown) => {
          if (scopeRef.current !== scope || controller.signal.aborted) return;
          setSnapshot({
            scope,
            loading: false,
            error: isPermissionError(cause) ? "permissionDenied" : "loadError",
          });
        });
    }
    return () => {
      controller.abort();
      requests.forEach((request) => request.abort());
      requests.clear();
    };
  }, [scope, connected, refresh]);

  const current = snapshot.scope === scope ? snapshot : undefined;
  const canManage =
    connected &&
    isAdmin &&
    current?.catalog?.can_manage === true &&
    deniedScope !== scope;
  const scopeReady = connected && Boolean(tenantId && spaceId);
  const scopedTasks = tasks.filter(
    (task) => task.tenant_id === tenantId && task.space_id === spaceId,
  );
  const selectedTask =
    selection.scope === scope
      ? scopedTasks.find((task) => task.task_id === selection.id)
      : undefined;
  const canOperate =
    scopeReady && ["owner", "admin", "member"].includes(tenantRole ?? "");

  // Stable callback prevents a parent re-render from reopening config requests.
  const denyRef = useRef<() => void>(() => undefined);
  denyRef.current = () => {
    setDeniedScope(scope);
    setDialog(null);
    setNotice({ scope, key: "permissionDenied" });
  };
  const [onDenied] = useState(() => () => denyRef.current());

  const checkConnection = async (id: ManagedComponentId) => {
    if (!canManage || (checking.scope === scope && checking.ids.has(id)))
      return;
    const controller = new AbortController();
    checkControllers.current.add(controller);
    setChecking((state) => ({
      scope,
      ids: new Set([...(state.scope === scope ? state.ids : []), id]),
    }));
    setNotice(null);
    try {
      const result = await componentManagementApi.check(id, controller.signal);
      if (scopeRef.current !== scope || controller.signal.aborted) return;
      setChecks((state) => ({
        scope,
        values: {
          ...(state.scope === scope ? state.values : {}),
          [id]: result,
        },
      }));
      onChanged?.();
    } catch (cause) {
      if (scopeRef.current !== scope || controller.signal.aborted) return;
      if (isPermissionError(cause)) onDenied();
      else setNotice({ scope, key: "checkError" });
    } finally {
      checkControllers.current.delete(controller);
      if (scopeRef.current === scope && !controller.signal.aborted) {
        setChecking((state) => {
          const ids = new Set(state.scope === scope ? state.ids : []);
          ids.delete(id);
          return { scope, ids };
        });
      }
    }
  };

  return (
    <div className={styles.center}>
      <div className={styles.header}>
        <div>
          <Typography.Title level={4}>
            {t("componentCenter.title")}
          </Typography.Title>
          <Typography.Text type="secondary">
            {t("componentCenter.subtitle")}
          </Typography.Text>
        </div>
        <Space wrap>
          {connected && isAdmin && canAccessPath("/admin/components") && (
            <Link to="/admin/components">
              {t("componentCenter.manageServices")}
            </Link>
          )}
          <Button
            icon={<RefreshCw size={15} />}
            loading={current?.loading}
            disabled={!connected}
            onClick={() => setRefresh((value) => value + 1)}
          >
            {t("componentCenter.refresh")}
          </Button>
        </Space>
      </div>
      {!connected && (
        <Alert
          type="info"
          showIcon
          message={t("componentCenter.offline")}
          action={
            <Button onClick={() => onNavigate("workflow")}>
              {t("componentCenter.editOffline")}
            </Button>
          }
        />
      )}
      {connected && !canManage && !current?.loading && (
        <Alert type="info" showIcon message={t("componentCenter.adminOnly")} />
      )}
      {current?.error && (
        <Alert
          type="error"
          showIcon
          message={t(`componentCenter.${current.error}`)}
        />
      )}
      {notice?.scope === scope && (
        <Alert
          type={notice.key === "saved" ? "success" : "error"}
          showIcon
          message={t(`componentCenter.${notice.key}`)}
        />
      )}
      {!scopeReady ? (
        <Alert
          type="info"
          showIcon
          message={t("componentCenter.selectScope")}
          action={
            <Button onClick={() => onNavigate("sync")}>
              {t("componentCenter.selectScopeAction")}
            </Button>
          }
        />
      ) : (
        <div className={styles.taskScope}>
          <Typography.Text strong>
            {t("componentCenter.selectTask")}
          </Typography.Text>
          <Select
            aria-label={t("componentCenter.selectTask")}
            className={styles.taskSelect}
            placeholder={t("componentCenter.selectTaskPlaceholder")}
            value={selectedTask?.task_id}
            allowClear
            showSearch
            optionFilterProp="label"
            onChange={(id: string | undefined) =>
              setSelection({ scope, id: id ?? "" })
            }
            options={scopedTasks.map((task) => ({
              value: task.task_id,
              label: task.goal,
            }))}
            notFoundContent={t("componentCenter.noTask")}
          />
          <Typography.Text type="secondary">
            {t("componentCenter.taskHint")}
          </Typography.Text>
          {!canOperate && (
            <Typography.Text type="warning">
              {t("componentCenter.viewerHint")}
            </Typography.Text>
          )}
          {scopedTasks.length === 0 && (
            <Button onClick={() => onNavigate("tasks")}>
              {t("componentCenter.createTask")}
            </Button>
          )}
        </div>
      )}
      <div className={styles.grid}>
        {COMPONENTS.map(({ id, name, icon: Icon }) => {
          const component = current?.catalog?.components.find(
            (item) => item.id === id,
          );
          const componentEnabled =
            Boolean(component) && component?.status !== "disabled";
          const check = checks.scope === scope ? checks.values[id] : undefined;
          const status = !connected
            ? "unknown"
            : check
            ? check.healthy
              ? "connected"
              : "error"
            : component?.status ?? "unknown";
          const operation = TASK_OPERATIONS[id];
          const operationAllowed =
            operation === "memory"
              ? scopeReady && Boolean(tenantRole)
              : canOperate;
          return (
            <Card key={id} className={styles.card} data-component-id={id}>
              <div className={styles.cardHeading}>
                <div className={styles.identity}>
                  <span className={styles.icon}>
                    {Icon ? (
                      <Icon size={21} />
                    ) : (
                      <img src={APP_LOGO_SRC} alt="" />
                    )}
                  </span>
                  <span>{name}</span>
                </div>
                <Tag
                  color={
                    [
                      "connected",
                      "available",
                      "ready",
                      "ok",
                      "healthy",
                    ].includes(status)
                      ? "success"
                      : ["error", "unavailable", "unhealthy"].includes(status)
                      ? "error"
                      : "default"
                  }
                >
                  {t(`componentCenter.statuses.${status}`, {
                    defaultValue: status,
                  })}
                </Tag>
              </div>
              <p className={styles.description}>
                {t(`componentCenter.components.${id}.description`)}
              </p>
              <Typography.Text type="secondary">
                {t(`componentCenter.components.${id}.capability`)}
              </Typography.Text>
              {component?.detail && !check && (
                <div className={styles.detail}>
                  {t(`componentCenter.details.${component.detail}`, {
                    defaultValue: component.detail,
                  })}
                </div>
              )}
              {check && (
                <div className={styles.detail} role="status">
                  {check.checks.map((item) => (
                    <div key={item.code}>
                      <Tag color={item.ok ? "success" : "error"}>
                        {item.ok ? "✓" : "!"}
                      </Tag>
                      {t(`componentCenter.checks.${item.code}`, {
                        defaultValue: item.code,
                      })}
                    </div>
                  ))}
                </div>
              )}
              <div className={styles.actions}>
                <Space wrap>
                  {onSession && (
                    <Button
                      disabled={!scopeReady || !tenantRole || !componentEnabled}
                      onClick={() =>
                        onSession({
                          component_id: id,
                          ...(selectedTask
                            ? { task_id: selectedTask.task_id }
                            : {}),
                        })
                      }
                    >
                      {t("operationSessions.entry")}
                    </Button>
                  )}
                  {id === "octop" &&
                    OCTOP_LINKS.filter(
                      ([, href]) => connected && canAccessPath(href),
                    ).map(([label, href]) => (
                      <Link key={href} to={href}>
                        {t(`componentCenter.actions.${label}`)}
                      </Link>
                    ))}
                  {(id === "octop" || id === "beatdesign") && (
                    <Button onClick={() => onNavigate("workflow")}>
                      {t("componentCenter.actions.workflow")}
                    </Button>
                  )}
                  {(id === "beatdesign" || id === "rna") && (
                    <Button
                      disabled={!scopeReady || !tenantRole || !componentEnabled}
                      onClick={() => setProjectDialog({ scope, id })}
                    >
                      {t("componentCenter.projectTools")}
                    </Button>
                  )}
                  {id === "temporal" && (
                    <Button onClick={() => onNavigate("tasks")}>
                      {t("componentCenter.actions.temporal")}
                    </Button>
                  )}
                  {id === "commerceagentbench" && (
                    <Button onClick={() => onNavigate("evaluation")}>
                      {t("evaluationCenter.title")}
                    </Button>
                  )}
                  {operation && (
                    <Button
                      type="primary"
                      disabled={
                        !operationAllowed || !selectedTask || !componentEnabled
                      }
                      onClick={() => {
                        if (
                          operationAllowed &&
                          selectedTask &&
                          componentEnabled
                        )
                          onOperation(operation, selectedTask);
                      }}
                    >
                      {t(`componentCenter.actions.${operation}`)}
                    </Button>
                  )}
                </Space>
              </div>
              <Space wrap>
                <Button
                  icon={<Settings2 size={14} />}
                  disabled={!canManage}
                  aria-label={t("componentCenter.configurationTitle", { name })}
                  onClick={() => setDialog({ scope, id })}
                >
                  {t("componentCenter.configure")}
                </Button>
                <Button
                  icon={<Unplug size={14} />}
                  disabled={!canManage}
                  loading={checking.scope === scope && checking.ids.has(id)}
                  aria-label={`${name} ${t("componentCenter.check")}`}
                  onClick={() => void checkConnection(id)}
                >
                  {t("componentCenter.check")}
                </Button>
              </Space>
            </Card>
          );
        })}
      </div>
      {dialog?.scope === scope && canManage && (
        <ConfigurationDialog
          key={`${scope}:${dialog.id}`}
          componentId={dialog.id}
          name={COMPONENTS.find((item) => item.id === dialog.id)!.name}
          onClose={() => setDialog(null)}
          onDenied={onDenied}
          onSaved={() => {
            if (scopeRef.current !== scope) return;
            setDialog(null);
            setNotice({ scope, key: "saved" });
            setChecks({ scope, values: {} });
            setRefresh((value) => value + 1);
            onChanged?.();
          }}
        />
      )}
      {projectDialog?.scope === scope &&
        scopeReady &&
        tenantRole &&
        current?.catalog?.components.some(
          (component) =>
            component.id === projectDialog.id &&
            component.status !== "disabled",
        ) && (
          <ComponentProjectDialog
            key={`${scope}:${projectDialog.id}`}
            componentId={projectDialog.id}
            tenantId={tenantId}
            spaceId={spaceId}
            canManage={canManage && ["owner", "admin"].includes(tenantRole)}
            onClose={() => setProjectDialog(null)}
            onChanged={onChanged}
            onReturnResult={
              onSession
                ? (result) => {
                    setProjectDialog(null);
                    onSession({
                      component_id: projectDialog.id,
                      operation: result.operation,
                      input: {},
                      initial_result: result.result,
                    });
                  }
                : undefined
            }
          />
        )}
    </div>
  );
}
