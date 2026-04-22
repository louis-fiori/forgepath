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

const scaffolderModuleIncidentAnalyzerAnalyzeLog = createBackendModule({
  pluginId: 'scaffolder',
  moduleId: 'incident-analyzer-analyze-log',
  register({ registerInit }) {
    registerInit({
      deps: { scaffolder: scaffolderActionsExtensionPoint },
      async init({ scaffolder }) {
        scaffolder.addActions(
          createTemplateAction({
            id: 'incident-analyzer:analyzeLog',
            description:
              "Calls the incident-analyzer's /analyze-log endpoint to analyze a single log line, pasted raw or fetched from Loki, and returns the structured diagnosis.",
            schema: {
              input: {
                namespace: z =>
                  z
                    .string()
                    .describe('Namespace the log belongs to (used for the runbook and the issue).'),
                logLine: z =>
                  z
                    .string()
                    .optional()
                    .describe('A raw log line to analyze. Mutually exclusive with query.'),
                query: z =>
                  z
                    .string()
                    .optional()
                    .describe('Substring to match in Loki (LogQL |= filter); the newest match is analyzed.'),
                window: z =>
                  z
                    .string()
                    .optional()
                    .describe('Loki lookback window for query mode, e.g. 10m. Defaults server-side.'),
                component: z =>
                  z
                    .string()
                    .optional()
                    .describe('Override the component attribution (defaults to the one parsed from the line).'),
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
                analyzedLine: z => z.string().optional(),
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
              const { namespace, window, component, openIssue } = ctx.input;
              // Unset template parameters arrive as empty strings, treat as absent.
              const logLine = ctx.input.logLine || undefined;
              const query = ctx.input.query || undefined;
              if (!logLine === !query) {
                throw new Error('Provide exactly one of logLine or query.');
              }

              const body: Record<string, unknown> = { namespace };
              if (logLine !== undefined) body.log_line = logLine;
              if (query !== undefined) body.query = query;
              if (window) body.window = window;
              if (component) body.component = component;
              if (openIssue !== undefined) body.open_issue = openIssue;

              ctx.logger.info(
                `Requesting single-log analysis for namespace=${namespace} (mode=${logLine ? 'paste' : 'loki'}, openIssue=${openIssue})`,
              );

              const headers: Record<string, string> = {
                'content-type': 'application/json',
              };
              if (ANALYZER_TOKEN) headers.authorization = `Bearer ${ANALYZER_TOKEN}`;

              const res = await fetch(`${ANALYZER_URL}/analyze-log`, {
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
              const line = r.candidate?.log_samples?.[0]?.samples?.[0];
              if (line) ctx.output('analyzedLine', line);
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
                ctx.logger.info(`No diagnosis: ${r.note ?? 'no log line analyzed'}`);
              }
            },
          }),
        );
      },
    });
  },
});

export default scaffolderModuleIncidentAnalyzerAnalyzeLog;
