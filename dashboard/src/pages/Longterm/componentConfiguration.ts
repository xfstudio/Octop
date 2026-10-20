import type {
  ComponentConfiguration,
  ComponentConfigurationPatch,
} from "../../api/modules/componentManagement";

/** Only changed values leave the browser; blank secret inputs preserve secrets. */
export function componentConfigurationPatch(
  configuration: ComponentConfiguration,
  values: Record<string, unknown>,
  clearSecrets: ReadonlySet<string>,
): ComponentConfigurationPatch {
  const patch: ComponentConfigurationPatch = {
    values: {},
    secrets: {},
    clear_secrets: [],
  };
  for (const field of configuration.fields) {
    const value = values[field.key];
    if (field.secret) {
      if (clearSecrets.has(field.key)) {
        patch.clear_secrets.push(field.key);
      } else if (typeof value === "string" && value.trim()) {
        patch.secrets[field.key] = value;
      }
    } else if (
      (typeof value === "string" || typeof value === "boolean") &&
      value !== configuration.values[field.key]
    ) {
      patch.values[field.key] = value;
    }
  }
  return patch;
}
