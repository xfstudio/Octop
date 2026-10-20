import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Checkbox,
  Form,
  Input,
  Modal,
  Spin,
  Switch,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  componentManagementApi,
  type ComponentConfiguration,
  type ManagedComponentId,
} from "../../api/modules/componentManagement";
import { componentConfigurationPatch } from "./componentConfiguration";
import { isComponentPermissionError as isPermissionError } from "./componentMetadata";
import styles from "./ComponentCenter.module.less";

export default function ConfigurationDialog({
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
