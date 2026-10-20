import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Modal, Space, Tag, Typography } from "antd";
import { ExternalLink, Power, RefreshCw, Settings2 } from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  componentManagementApi,
  ComponentLifecycleError,
  type ComponentCatalog,
  type ComponentLifecycleAction,
  type ComponentRuntime,
  type ManagedComponentId,
} from "../../../api/modules/componentManagement";
import { APP_LOGO_SRC } from "../../../assets/brand";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import PageShell from "../../../layouts/PageShell";
import ConfigurationDialog from "../../Longterm/ComponentConfigurationDialog";
import {
  COMPONENTS,
  isComponentPermissionError,
} from "../../Longterm/componentMetadata";
import styles from "../../Longterm/ComponentCenter.module.less";
import serviceStyles from "./index.module.less";

const prefix = "componentCenter.services";
const managedProcess = (mode: ComponentRuntime["mode"]) =>
  mode === "compose" || mode === "process";

function consoleHref(runtime: ComponentRuntime): string | null {
  if (!runtime.console.available || !runtime.console.url) return null;
  try {
    const url = new URL(runtime.console.url, window.location.origin);
    if (url.username || url.password) return null;
    if (runtime.console.kind === "internal") {
      if (
        !runtime.console.url.startsWith("/") ||
        runtime.console.url.startsWith("//") ||
        runtime.console.url.includes("\\") ||
        url.origin !== window.location.origin
      )
        return null;
      return `${url.pathname}${url.search}${url.hash}`;
    }
    return /^https?:\/\//i.test(runtime.console.url) &&
      ["http:", "https:"].includes(url.protocol) &&
      !url.search &&
      !url.hash &&
      !runtime.console.url.includes("\\")
      ? url.href
      : null;
  } catch {
    return null;
  }
}

interface ManagerProps {
  isAdmin: boolean;
  connected: boolean;
}

/** Parent keys this subtree by actor, role and connectivity to discard old work. */
export function ComponentServicesManager({ isAdmin, connected }: ManagerProps) {
  const { t } = useTranslation();
  const [catalog, setCatalog] = useState<ComponentCatalog | null>(null);
  const [runtimes, setRuntimes] = useState<ComponentRuntime[]>([]);
  const [loading, setLoading] = useState(false);
  const [ready, setReady] = useState(false);
  const [denied, setDenied] = useState(false);
  const [notice, setNotice] = useState<{ key: string; reason?: string } | null>(
    null,
  );
  const [configuration, setConfiguration] = useState<ManagedComponentId | null>(
    null,
  );
  const [confirmation, setConfirmation] = useState<{
    runtime: ComponentRuntime;
    action: ComponentLifecycleAction;
  } | null>(null);
  const [busy, setBusy] = useState<ManagedComponentId | null>(null);
  const active = useRef(true);
  const locked = useRef(false);
  const loadGeneration = useRef(0);
  const controllers = useRef(new Set<AbortController>());

  const denyRef = useRef<() => void>(() => undefined);
  denyRef.current = () => {
    setDenied(true);
    setConfiguration(null);
    setConfirmation(null);
    setReady(false);
    setNotice({ key: "permissionDenied" });
  };
  const [onDenied] = useState(() => () => denyRef.current());

  const refresh = useCallback(async () => {
    if (!isAdmin || !connected || !active.current) return;
    const generation = ++loadGeneration.current;
    const controller = new AbortController();
    controllers.current.add(controller);
    setLoading(true);
    setReady(false);
    try {
      const [nextCatalog, runtime] = await Promise.all([
        componentManagementApi.catalog(controller.signal),
        componentManagementApi.runtime(controller.signal),
      ]);
      if (
        !active.current ||
        controller.signal.aborted ||
        generation !== loadGeneration.current
      )
        return;
      setCatalog(nextCatalog);
      setRuntimes(runtime.components);
      setReady(true);
      if (!nextCatalog.can_manage) onDenied();
    } catch (cause) {
      if (
        !active.current ||
        controller.signal.aborted ||
        generation !== loadGeneration.current
      )
        return;
      if (isComponentPermissionError(cause)) onDenied();
      else setNotice({ key: "loadError" });
    } finally {
      controllers.current.delete(controller);
      if (active.current && generation === loadGeneration.current)
        setLoading(false);
    }
  }, [connected, isAdmin, onDenied]);

  useEffect(() => {
    active.current = true;
    const requests = controllers.current;
    void refresh();
    return () => {
      active.current = false;
      requests.forEach((request) => request.abort());
      requests.clear();
    };
  }, [refresh]);

  const canManage =
    isAdmin && connected && !denied && catalog?.can_manage === true;

  const perform = async () => {
    if (!confirmation || !canManage || !ready || locked.current) return;
    locked.current = true;
    const { runtime, action } = confirmation;
    const controller = new AbortController();
    controllers.current.add(controller);
    setBusy(runtime.component_id);
    setNotice(null);
    try {
      await componentManagementApi.lifecycle(
        runtime.component_id,
        action,
        runtime.revision,
        controller.signal,
      );
      if (!active.current || controller.signal.aborted) return;
      setNotice({ key: "operationCompleted" });
    } catch (cause) {
      if (!active.current || controller.signal.aborted) return;
      if (
        cause instanceof ComponentLifecycleError &&
        [401, 403].includes(cause.status ?? 0)
      )
        onDenied();
      else
        setNotice({
          key:
            cause instanceof ComponentLifecycleError && cause.status === 409
              ? "conflict"
              : "operationFailed",
          reason:
            cause instanceof ComponentLifecycleError ? cause.reason : undefined,
        });
    } finally {
      controllers.current.delete(controller);
      if (active.current && !controller.signal.aborted) {
        setConfirmation(null);
        // Read the actual state after every outcome; never retry the POST.
        await refresh();
        if (active.current) {
          setBusy(null);
          locked.current = false;
        }
      }
    }
  };

  const actionText = (
    runtime: ComponentRuntime,
    action: ComponentLifecycleAction,
  ) =>
    t(
      `${prefix}.${
        managedProcess(runtime.mode)
          ? action
          : action === "start"
          ? "enable"
          : "disable"
      }`,
    );

  if (!isAdmin)
    return <Alert type="error" showIcon message={t(`${prefix}.adminOnly`)} />;

  return (
    <PageShell
      title={t(`${prefix}.title`)}
      subtitle={t(`${prefix}.subtitle`)}
      actions={
        <Space wrap>
          <Link to="/workbench/platform?section=integrations">
            {t(`${prefix}.workbench`)}
          </Link>
          <Button
            icon={<RefreshCw size={15} />}
            loading={loading}
            disabled={!connected || Boolean(busy) || denied}
            onClick={() => {
              setNotice(null);
              void refresh();
            }}
          >
            {t("componentCenter.refresh")}
          </Button>
        </Space>
      }
    >
      <div className={styles.center}>
        {!connected && (
          <Alert type="info" showIcon message={t(`${prefix}.offline`)} />
        )}
        <Alert type="info" showIcon message={t(`${prefix}.scopeHint`)} />
        {notice && (
          <Alert
            type={notice.key === "operationCompleted" ? "info" : "error"}
            showIcon
            message={t(`${prefix}.${notice.key}`)}
            description={
              notice.reason
                ? t(`${prefix}.reasons.${notice.reason}`, {
                    defaultValue: t(`${prefix}.reasonFallback`),
                  })
                : undefined
            }
          />
        )}
        <div className={styles.grid}>
          {COMPONENTS.map(({ id, name, icon: Icon }) => {
            const runtime = ready
              ? runtimes.find((entry) => entry.component_id === id)
              : undefined;
            const href = runtime ? consoleHref(runtime) : null;
            const state = runtime?.state ?? "unknown";
            const stateColor = ["running", "enabled"].includes(state)
              ? "success"
              : ["partial", "starting", "stopping"].includes(state)
              ? "warning"
              : "default";
            return (
              <Card
                key={id}
                className={styles.card}
                data-service-component={id}
              >
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
                  <Tag color={stateColor}>{t(`${prefix}.states.${state}`)}</Tag>
                </div>
                <p className={styles.description}>
                  {t(`componentCenter.components.${id}.description`)}
                </p>
                {runtime && (
                  <div className={serviceStyles.runtimeDetails}>
                    <Typography.Text type="secondary">
                      {t(`${prefix}.modes.${runtime.mode}`)}
                    </Typography.Text>
                    <Typography.Text type="secondary">
                      {t(`${prefix}.integrationState`, {
                        state: t(
                          `${prefix}.states.${
                            runtime.enabled ? "enabled" : "disabled"
                          }`,
                        ),
                      })}
                    </Typography.Text>
                    {runtime.mode === "external" && (
                      <Typography.Text type="secondary">
                        {t(`${prefix}.externalHint`)}
                      </Typography.Text>
                    )}
                    {runtime.reason && (
                      <Typography.Text
                        type={
                          ["runtime_embedded", "runtime_external"].includes(
                            runtime.reason,
                          )
                            ? "secondary"
                            : "warning"
                        }
                      >
                        {t(`${prefix}.reasons.${runtime.reason}`, {
                          defaultValue: t(`${prefix}.reasonFallback`),
                        })}
                      </Typography.Text>
                    )}
                    {runtime.services.length > 0 && (
                      <ul
                        className={serviceStyles.services}
                        aria-label={t(`${prefix}.serviceList`)}
                      >
                        {runtime.services.map((service) => (
                          <li key={service.name}>
                            <span>{service.name}</span>
                            <span>
                              {t(`${prefix}.states.${service.state}`, {
                                defaultValue: t(`${prefix}.states.unknown`),
                              })}
                              {service.health
                                ? ` · ${t(
                                    `${prefix}.health.${service.health}`,
                                    {
                                      defaultValue: t(
                                        `${prefix}.health.unknown`,
                                      ),
                                    },
                                  )}`
                                : ""}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
                <div className={styles.actions}>
                  <Space wrap>
                    <Button
                      icon={<Settings2 size={14} />}
                      disabled={!canManage || Boolean(busy)}
                      aria-label={t("componentCenter.configurationTitle", {
                        name,
                      })}
                      onClick={() => setConfiguration(id)}
                    >
                      {t("componentCenter.configure")}
                    </Button>
                    {(["start", "stop"] as const).map((action) => (
                      <Button
                        key={action}
                        icon={<Power size={14} />}
                        danger={action === "stop"}
                        loading={busy === id && confirmation?.action === action}
                        disabled={
                          !canManage ||
                          !runtime ||
                          Boolean(busy) ||
                          !(action === "start"
                            ? runtime.can_start
                            : runtime.can_stop)
                        }
                        aria-label={`${name} ${
                          runtime
                            ? actionText(runtime, action)
                            : t(`${prefix}.${action}`)
                        }`}
                        onClick={() =>
                          runtime && setConfirmation({ runtime, action })
                        }
                      >
                        {runtime
                          ? actionText(runtime, action)
                          : t(`${prefix}.${action}`)}
                      </Button>
                    ))}
                    {canManage && href && runtime ? (
                      runtime.console.kind === "internal" ? (
                        <Link to={href}>
                          {t(
                            `${prefix}.${
                              runtime.console.label === "native"
                                ? "nativeConsole"
                                : "integrationConsole"
                            }`,
                          )}
                        </Link>
                      ) : (
                        <a
                          href={href}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <Space size={4}>
                            {t(
                              `${prefix}.${
                                runtime.console.label === "native"
                                  ? "nativeConsole"
                                  : "integrationConsole"
                              }`,
                            )}
                            <ExternalLink size={13} />
                          </Space>
                        </a>
                      )
                    ) : (
                      <Typography.Text type="secondary">
                        {t(`${prefix}.consoleUnavailable`)}
                      </Typography.Text>
                    )}
                  </Space>
                </div>
              </Card>
            );
          })}
        </div>
      </div>
      {configuration && canManage && (
        <ConfigurationDialog
          componentId={configuration}
          name={COMPONENTS.find((item) => item.id === configuration)!.name}
          onClose={() => setConfiguration(null)}
          onDenied={onDenied}
          onSaved={() => {
            setConfiguration(null);
            void refresh();
          }}
        />
      )}
      {confirmation && canManage && (
        <Modal
          open
          title={t(`${prefix}.confirmTitle`, {
            action: actionText(confirmation.runtime, confirmation.action),
            name: confirmation.runtime.name,
          })}
          onCancel={() => {
            if (!busy) setConfirmation(null);
          }}
          onOk={() => void perform()}
          confirmLoading={Boolean(busy)}
          okText={t(`${prefix}.confirmAction`)}
          cancelText={t("componentCenter.cancel")}
          okButtonProps={{
            danger: confirmation.action === "stop",
            disabled: !ready,
          }}
          cancelButtonProps={{ disabled: Boolean(busy) }}
          closable={!busy}
          maskClosable={!busy}
          keyboard={!busy}
        >
          <Typography.Paragraph>
            {t(
              `${prefix}.${
                confirmation.action === "stop" ? "stopWarning" : "startWarning"
              }`,
            )}
          </Typography.Paragraph>
          <Typography.Text type="secondary">
            {t(`${prefix}.confirmScope`)}
          </Typography.Text>
        </Modal>
      )}
    </PageShell>
  );
}

export default function ComponentServicesPage() {
  const user = useCurrentUser();
  const [connected, setConnected] = useState(navigator.onLine);
  useEffect(() => {
    const update = () => setConnected(navigator.onLine);
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);
  return (
    <ComponentServicesManager
      key={JSON.stringify([user?.id, user?.role, connected])}
      isAdmin={user?.role === "admin"}
      connected={connected}
    />
  );
}
