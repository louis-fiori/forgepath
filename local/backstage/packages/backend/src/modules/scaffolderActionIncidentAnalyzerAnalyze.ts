import { createBackendModule } from '@backstage/backend-plugin-api';
import {
  createTemplateAction,
  scaffolderActionsExtensionPoint,
} from '@backstage/plugin-scaffolder-node';

// In-cluster address of the incident-analyzer Service, overridable via env.
const ANALYZER_URL =
  process.env.INCIDENT_ANALYZER_URL ||
  'http://incident-analyzer.incident-analyzer.svc.cluster.local';

// Shared s2s bearer token gating /analyze* and /settings. Sent when set;
// omitted in token-less local/dev setups where the analyzer leaves auth open.
const ANALYZER_TOKEN = process.env.BACKSTAGE_S2S_TOKEN || '';

const scaffolderModuleIncidentAnalyzerAnalyze = createBackendModule({
  pluginId: 'scaffolder',
  moduleId: 'incident-analyzer-analyze',
  register({ registerInit }) {
    registerInit({
      deps: { scaffolder: scaffolderActionsExtensionPoint },
      async init({ scaffolder }) {
        scaffolder.addActions(
          createTemplateAction({
            id: 'incident-analyzer:analyze',
            description:
              "Calls the incident-analyzer's /analyze endpoint to run an on-demand incident analysis for a namespace and returns the structured diagnosis.",
            schema: {
              input: {
                namespace: z =>
                  z
                    .string()
                    .describe('Namespace to analyze (e.g. incident-generator).'),
                window: z =>
                  z
                    .string()
                    .optional()
                    .describe('LogQL lookback window, e.g. 10m. Defaults server-side.'),
                force: z =>
                  z
                    .boolean()
                    .optional()
                    .describe('Bypass the dedup cache and analyze even if recently filed.'),
                openIssue: z =>
                  z
                    .boolean()
                    .optional()
                    .describe('Override issue creation for this run (defaults to the analyzer global).'),
              },
              output: {
                detected: z => z.boolean(),
                deduped: z => z.boolean().optional(),
                note: z => z.string().optional(),
                summary: z => z.string().optional(),
                severity: z => z.string().optional(),
                affectedComponent: z => z.string().optional(),
                probableRootCause: z => z.string().optional(),
                recommendedRemediation: z => z.string().optional(),
                confidence: z => z.number().optional(),
                issueUrl: z => z.string().optional(),
                notified: z => z.boolean().optional(),
              },
            },
            async handler(ctx) {
              const { namespace, window, force, openIssue } = ctx.input;
              const body: Record<string, unknown> = { namespace };
              if (window !== undefined) body.window = window;
              if (force !== undefined) body.force = force;
              if (openIssue !== undefined) body.open_issue = openIssue;

              ctx.logger.info(
                `Requesting analysis for namespace=${namespace} (force=${!!force}, openIssue=${openIssue})`,
              );

              const headers: Record<string, string> = {
                'content-type': 'application/json',
              };
              if (ANALYZER_TOKEN) headers.authorization = `Bearer ${ANALYZER_TOKEN}`;

              const res = await fetch(`${ANALYZER_URL}/analyze`, {
                method: 'POST',
                headers,
                body: JSON.stringify(body),
              });
              if (!res.ok) {
                throw new Error(
                  `incident-analyzer returned ${res.status}: ${await res.text()}`,
                );
              }
              const r: any = await res.json();

              ctx.output('detected', !!r.detected);
              ctx.output('deduped', !!r.deduped);
              if (r.note) ctx.output('note', r.note);
              if (r.issue_url) ctx.output('issueUrl', r.issue_url);
              ctx.output('notified', !!r.notified);
              const d = r.diagnosis;
              if (d) {
                ctx.output('summary', d.summary);
                ctx.output('severity', d.severity);
                ctx.output('affectedComponent', d.affected_component);
                ctx.output('probableRootCause', d.probable_root_cause);
                ctx.output('recommendedRemediation', d.recommended_remediation);
                ctx.output('confidence', d.confidence);
                ctx.logger.info(`Diagnosis: [${d.severity}] ${d.summary}`);
              } else {
                ctx.logger.info(`No diagnosis: ${r.note ?? 'no incident detected'}`);
              }
            },
          }),
        );
      },
    });
  },
});

export default scaffolderModuleIncidentAnalyzerAnalyze;
