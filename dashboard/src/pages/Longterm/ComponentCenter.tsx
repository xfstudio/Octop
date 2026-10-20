import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import {
  Brain,
  Dna,
  Fingerprint,
  Gauge,
  RefreshCw,
  Settings2,
  Timer,
  Unplug,
  Workflow,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { APP_LOGO_SRC } from "../../assets/brand";
import {
  componentManagementApi,
  type ComponentCatalog,
  type ComponentConfiguration,
  type ComponentConnectionCheck,
  type ManagedComponentId,
  type ProjectComponentId,
} from "../../api/modules/componentManagement";
import type { LongtermTask, TaskOperation } from "../../api/modules/longterm";
import { componentConfigurationPatch } from "./componentConfiguration";
import ComponentProjectDialog from "./ComponentProjectDialog";
import styles from "./ComponentCenter.module.less";

export type ComponentCenterTab = "workflow" | "sync" | "tasks" | "integrations";

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
}

const COMPONENTS = [
  { id: "octop", name: "Octop", icon: null },
  { id: "semantica", name: "Semantica", icon: Fingerprint },
  { id: "beatdesign", name: "BeatDesign", icon: Workflow },
  { id: "commerceagentbench", name: "CommerceAgentBench", icon: Gauge },
  { id: "rna", name: "RNA", icon: Dna },
  { id: "hindsight", name: "Hindsight", icon: Brain },
  { id: "temporal", name: "Temporal", icon: Timer },
] as const;

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

const isPermissionError = (error: unknown) =>
  error instanceof Error && /Request failed: (401|403)\b/.test(error.message);

function ConfigurationDialog({
  componentId,
  name,
  onClose,
  onSaved,
  onDenied,
}: {
  componentId: ManagedComponentId;
  name: string;
  onClose: () => void;
  onSaved: () => void;
  onDenied: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<Record<string, string | boolean>>();
  const [configuration, setConfiguration] =
    useState<ComponentConfiguration | null>(null);
  const [clearSecrets, setClearSecrets] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(true);
  const controllers = useRef(new Set<AbortController>());

  useEffect(() => {
    active.current = true;
    const controller = new AbortController();
    const requests = controllers.current;
    requests.add(controller);
    void componentManagementApi
      .configuration(componentId, controller.signal)
      .then((result) => {
        if (!active.current || controller.signal.aborted) return;
        setConfiguration(result);
        // Secrets are never initialized from any server response.
        form.setFieldsValue(
          Object.fromEntries(
            result.fields.map((field) => [
              field.key,
              field.secret
                ? ""
                : result.values[field.key] ??
                  (field.type === "boolean" ? false : ""),
            ]),
          ),
        );
      })
      .catch((cause: unknown) => {
        if (!active.current || controller.signal.aborted) return;
        if (isPermissionError(cause)) onDenied();
        else setError("loadError");
      })
      .finally(() => {
        requests.delete(controller);
        if (active.current && !controller.signal.aborted) setLoading(false);
      });
    return () => {
      active.current = false;
      requests.forEach((request) => request.abort());
      requests.clear();
      form.resetFields();
    };
  }, [componentId, form, onDenied]);

  const save = async () => {
    if (!configuration || loading || saving) return;
    let values: Record<string, string | boolean>;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    if (!active.current) return;
    const controller = new AbortController();
    controllers.current.add(controller);
    setSaving(true);
    setError("");
    try {
      await componentManagementApi.save(
        componentId,
        componentConfigurationPatch(configuration, values, clearSecrets),
        controller.signal,
      );
      if (active.current && !controller.signal.aborted) onSaved();
    } catch (cause) {
      if (!active.current || controller.signal.aborted) return;
      if (isPermissionError(cause)) onDenied();
      else setError("saveError");
    } finally {
      controllers.current.delete(controller);
      if (active.current && !controller.signal.aborted) setSaving(false);
    }
  };

  return (
    <Modal
      open
      title={t("componentCenter.configurationTitle", { name })}
      onCancel={onClose}
      onOk={() => void save()}
      okText={t("componentCenter.save")}
      cancelText={t("componentCenter.cancel")}
      confirmLoading={saving}
      okButtonProps={{ disabled: loading || !configuration }}
      width={640}
    >
      <Alert
        type="info"
        showIcon
        message={t("componentCenter.configurationHint")}
        description={
          <>
            <div>{t("componentCenter.secretHint")}</div>
            {componentId === "temporal" && (
              <div>{t("componentCenter.temporalWorkerHint")}</div>
            )}
          </>
        }
      />
      {error && (
        <Alert type="error" showIcon message={t(`componentCenter.${error}`)} />
      )}
      {loading ? (
        <Spin />
      ) : (
        <Form form={form} layout="vertical" className={styles.form}>
          {configuration?.fields.length === 0 && (
            <Typography.Paragraph>
              {t("componentCenter.noFields")}
            </Typography.Paragraph>
          )}
          {configuration?.fields.map((field) => {
            const label = t(`componentCenter.fields.${field.key}`, {
              defaultValue: field.key,
            });
            const secretSet = Boolean(
              configuration.secret_configured[field.key],
            );
            return (
              <div key={field.key}>
                <Form.Item
                  name={field.key}
                  label={label}
                  valuePropName={
                    field.type === "boolean" && !field.secret
                      ? "checked"
                      : "value"
                  }
                  rules={
                    field.required &&
                    field.type !== "boolean" &&
                    (!field.secret || !secretSet) &&
                    !clearSecrets.has(field.key)
                      ? [
                          {
                            required: true,
                            message: t("componentCenter.fieldRequired"),
                          },
                        ]
                      : []
                  }
                  extra={
                    field.secret ? (
                      <div className={styles.secretStatus}>
                        <span>
                          {t(
                            `componentCenter.${
                              secretSet ? "secretSet" : "secretUnset"
                            }`,
                          )}
                        </span>
                        {secretSet && (
                          <Checkbox
                            aria-label={`${label} ${t(
                              "componentCenter.clearSecret",
                            )}`}
                            checked={clearSecrets.has(field.key)}
                            disabled={saving}
                            onChange={(event) =>
                              setClearSecrets((current) => {
                                const next = new Set(current);
                                if (event.target.checked) next.add(field.key);
                                else next.delete(field.key);
                                return next;
                              })
                            }
                          >
                            {t("componentCenter.clearSecret")}
                          </Checkbox>
                        )}
                      </div>
                    ) : undefined
                  }
                >
                  {field.secret ? (
                    <Input.Password
                      autoComplete="new-password"
                      disabled={saving || clearSecrets.has(field.key)}
                      placeholder={t("componentCenter.secretPlaceholder")}
                    />
                  ) : field.type === "boolean" ? (
                    <Switch disabled={saving} />
                  ) : field.key.endsWith("json") ? (
                    <Input.TextArea rows={5} disabled={saving} />
                  ) : (
                    <Input disabled={saving} autoComplete="off" />
                  )}
                </Form.Item>
                {clearSecrets.has(field.key) && (
                  <Typography.Paragraph type="warning">
                    {t("componentCenter.clearSecretHint")}
                  </Typography.Paragraph>
                )}
              </div>
            );
          })}
        </Form>
      )}
    </Modal>
  );
}

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
        <Button
          icon={<RefreshCw size={15} />}
          loading={current?.loading}
          disabled={!connected}
          onClick={() => setRefresh((value) => value + 1)}
        >
          {t("componentCenter.refresh")}
        </Button>
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
          />
        )}
    </div>
  );
}
