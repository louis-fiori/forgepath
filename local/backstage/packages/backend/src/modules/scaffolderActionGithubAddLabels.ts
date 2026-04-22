import { createBackendModule } from '@backstage/backend-plugin-api';
import {
  createTemplateAction,
  scaffolderActionsExtensionPoint,
} from '@backstage/plugin-scaffolder-node';
import { Octokit } from '@octokit/rest';

function parseRepoUrl(repoUrl: string): { owner: string; repo: string } {
  const url = new URL(`https://${repoUrl}`);
  const owner = url.searchParams.get('owner');
  const repo = url.searchParams.get('repo');
  if (!owner || !repo) {
    throw new Error(
      `repoUrl missing owner or repo: ${repoUrl} (expected github.com?owner=X&repo=Y)`,
    );
  }
  return { owner, repo };
}

const scaffolderModuleGithubAddLabels = createBackendModule({
  pluginId: 'scaffolder',
  moduleId: 'github-add-labels',
  register({ registerInit }) {
    registerInit({
      deps: { scaffolder: scaffolderActionsExtensionPoint },
      async init({ scaffolder }) {
        scaffolder.addActions(
          createTemplateAction({
            id: 'github:addLabels',
            description:
              'Adds one or more labels to a GitHub PR or issue. Used as a post-step to publish:github:pull-request since that action does not accept labels in its input schema.',
            schema: {
              input: {
                repoUrl: z =>
                  z
                    .string()
                    .describe(
                      'Backstage-format repo URL: github.com?owner=X&repo=Y',
                    ),
                number: z =>
                  z
                    .number()
                    .describe('PR or issue number to label.'),
                labels: z =>
                  z
                    .array(z.string())
                    .describe(
                      'Labels to add. Missing labels on the repo are auto-created by GitHub.',
                    ),
              },
            },
            async handler(ctx) {
              const { repoUrl, number, labels } = ctx.input;
              const { owner, repo } = parseRepoUrl(repoUrl);

              const token = process.env.GITHUB_TOKEN;
              if (!token) {
                throw new Error(
                  'GITHUB_TOKEN env var not set on the Backstage backend.',
                );
              }

              const octokit = new Octokit({ auth: token });
              ctx.logger.info(
                `Adding labels [${labels.join(', ')}] to ${owner}/${repo}#${number}`,
              );
              await octokit.issues.addLabels({
                owner,
                repo,
                issue_number: number,
                labels,
              });
            },
          }),
        );
      },
    });
  },
});

export default scaffolderModuleGithubAddLabels;
