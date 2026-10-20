import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  componentManagementApi,
  type ComponentProjectBinding,
  type ComponentProjectRead,
  type ProjectComponentId,
  type ProjectReadOperation,
} from "../../api/modules/componentManagement";
import styles from "./ComponentCenter.module.less";

const OPERATIONS: Record<ProjectComponentId, ProjectReadOperation[]> = {
  beatdesign: [
    "project",
    "canvas",
    "assets",
    "skills",
    "generation_models",
    "generation_history",
    "editor",
    "editor_diagnostics",
  ],
  rna: ["genes", "observability"],
};

function isPermissionError(error: unknown) {
  return (
    error instanceof Error && /Request failed: (401|403)\b/.test(error.message)
  );
}

export default function ComponentProjectDialog({
  componentId,
  tenantId,
  spaceId,
  canManage,
  onClose,
  onChanged,
}: {
  componentId: ProjectComponentId;
  tenantId: string;
  spaceId: string;
  canManage: boolean;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const { t } = useTranslation();
  const [project, setProject] = useState<ComponentProjectBinding | null>(null);
  const [projectId, setProjectId] = useState("");
  const [operation, setOperation] = useState(OPERATIONS[componentId][0]);
  const [result, setResult] = useState<ComponentProjectRead | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [reading, setReading] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const active = useRef(true);
  const controllers = useRef(new Set<AbortController>());

  useEffect(() => {
    active.current = true;
    const controller = new AbortController();
    const requests = controllers.current;
    requests.add(controller);
    void componentManagementApi
      .project(tenantId, componentId, controller.signal)
      .then((binding) => {
        if (!active.current || controller.signal.aborted) return;
        setProject(binding);
        setProjectId(binding.project_id ?? "");
      })
      .catch((cause: unknown) => {
        if (!active.current || controller.signal.aborted) return;
        setError(isPermissionError(cause) ? "permissionDenied" : "loadError");
        if (isPermissionError(cause)) setForbidden(true);
      })
      .finally(() => {
        requests.delete(controller);
        if (active.current && !controller.signal.aborted) setLoading(false);
      });
    return () => {
      active.current = false;
      requests.forEach((request) => request.abort());
      requests.clear();
    };
  }, [componentId, tenantId]);

  const save = async () => {
    if (
      !project ||
      !project.can_manage ||
      !canManage ||
      saving ||
      reading ||
      forbidden
    )
      return;
    const controller = new AbortController();
    controllers.current.add(controller);
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const binding = await componentManagementApi.bindProject(
        tenantId,
        componentId,
        {
          project_id: projectId.trim() || null,
          expected_revision: project.revision,
        },
        controller.signal,
      );
      if (!active.current || controller.signal.aborted) return;
      setProject(binding);
      setProjectId(binding.project_id ?? "");
      setResult(null);
      setNotice("projectSaved");
      onChanged?.();
    } catch (cause) {
      if (!active.current || controller.signal.aborted) return;
      if (isPermissionError(cause)) {
        setForbidden(true);
        setResult(null);
        setProject(null);
        setError("permissionDenied");
      } else if (
        cause instanceof Error &&
        /Request failed: 409\b/.test(cause.message)
      ) {
        setError("projectConflict");
      } else setError("saveError");
    } finally {
      controllers.current.delete(controller);
      if (active.current && !controller.signal.aborted) setSaving(false);
    }
  };

  const read = async () => {
    if (!project?.project_id || saving || reading || forbidden) return;
    const controller = new AbortController();
    controllers.current.add(controller);
    setReading(true);
    setResult(null);
    setError("");
    setNotice("");
    try {
      const response = await componentManagementApi.readProject(
        tenantId,
        spaceId,
        componentId,
        operation,
        controller.signal,
      );
      if (!active.current || controller.signal.aborted) return;
      setResult(response);
    } catch (cause) {
      if (!active.current || controller.signal.aborted) return;
      if (isPermissionError(cause)) {
        setForbidden(true);
        setProject(null);
        setError("permissionDenied");
      } else setError("projectReadError");
    } finally {
      controllers.current.delete(controller);
      if (active.current && !controller.signal.aborted) setReading(false);
    }
  };

  const editable = canManage && project?.can_manage === true && !forbidden;
  const bindingChanged = (project?.project_id ?? "") !== projectId.trim();
  const name = componentId === "beatdesign" ? "BeatDesign" : "RNA";
  return (
    <Modal
      open
      title={t("componentCenter.projectTitle", { name })}
      onCancel={onClose}
      footer={<Button onClick={onClose}>{t("componentCenter.close")}</Button>}
      width={800}
    >
      <Alert
        type="info"
        showIcon
        message={t("componentCenter.projectHint")}
        description={t("componentCenter.projectBindingPermission")}
      />
      {error && (
        <Alert type="error" showIcon message={t(`componentCenter.${error}`)} />
      )}
      {notice && (
        <Alert
          type="success"
          showIcon
          message={t(`componentCenter.${notice}`)}
        />
      )}
      {loading ? (
        <Spin />
      ) : (
        <Form layout="vertical" className={styles.form}>
          <Form.Item label={t("componentCenter.projectId")}>
            <Input
              aria-label={t("componentCenter.projectId")}
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
              disabled={!editable || saving || reading}
              placeholder={t("componentCenter.projectIdPlaceholder")}
              maxLength={240}
              autoComplete="off"
            />
          </Form.Item>
          {editable && (
            <Button
              loading={saving}
              disabled={reading || !bindingChanged}
              onClick={() => void save()}
            >
              {t("componentCenter.saveProject")}
            </Button>
          )}
          {editable && bindingChanged && (
            <Typography.Paragraph type="secondary">
              {t("componentCenter.projectSaveFirst")}
            </Typography.Paragraph>
          )}
          {!project?.project_id && (
            <Typography.Paragraph type="secondary">
              {t("componentCenter.projectRequired")}
            </Typography.Paragraph>
          )}
          <Form.Item label={t("componentCenter.projectOperation")}>
            <Space wrap className={styles.readToolbar}>
              <Select
                aria-label={t("componentCenter.projectOperation")}
                value={operation}
                onChange={(value: ProjectReadOperation) => {
                  setOperation(value);
                  setResult(null);
                }}
                options={OPERATIONS[componentId].map((value) => ({
                  value,
                  label: t(`componentCenter.projectOperations.${value}`),
                }))}
                disabled={reading || saving || forbidden}
                className={styles.operationSelect}
              />
              <Button
                type="primary"
                loading={reading}
                disabled={
                  !project?.project_id || saving || forbidden || bindingChanged
                }
                onClick={() => void read()}
              >
                {t("componentCenter.readProject")}
              </Button>
            </Space>
          </Form.Item>
          {result && (
            <div>
              <Typography.Text strong>
                {t("componentCenter.projectResult", {
                  operation: t(
                    `componentCenter.projectOperations.${result.operation}`,
                  ),
                })}
              </Typography.Text>
              <Typography.Paragraph type="secondary">
                {t("componentCenter.readProjectId", {
                  projectId: result.project_id,
                })}
              </Typography.Paragraph>
              <pre className={styles.result}>
                {JSON.stringify(result.result, null, 2)}
              </pre>
            </div>
          )}
        </Form>
      )}
    </Modal>
  );
}
