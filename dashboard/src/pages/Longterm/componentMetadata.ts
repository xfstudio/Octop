import { Brain, Dna, Fingerprint, Gauge, Timer, Workflow } from "lucide-react";

export const COMPONENTS = [
  { id: "octop", name: "Octop", icon: null },
  { id: "semantica", name: "Semantica", icon: Fingerprint },
  { id: "beatdesign", name: "BeatDesign", icon: Workflow },
  { id: "commerceagentbench", name: "CommerceAgentBench", icon: Gauge },
  { id: "rna", name: "RNA", icon: Dna },
  { id: "hindsight", name: "Hindsight", icon: Brain },
  { id: "temporal", name: "Temporal", icon: Timer },
] as const;

export const isComponentPermissionError = (error: unknown) =>
  error instanceof Error && /Request failed: (401|403)\b/.test(error.message);
