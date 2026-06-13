// Hand-written until `npm run gen:api` produces schema.d.ts from the backend's
// OpenAPI document. The frontend depends only on these shapes — never on backend
// internals (monorepo, hard-separated).

export interface ProviderView {
  name: string;
  display_name: string;
  auth_kind: string;
  official: boolean;
  stability: string;
  has_isrc: boolean;
  can_source: boolean;
  can_target: boolean;
  warning: string | null;
}

export interface JobView {
  id: string;
  status: string;
  total: number;
  done: number;
  failed: number;
}

export interface ProgressEvent {
  job_id: string;
  cursor: number;
}
