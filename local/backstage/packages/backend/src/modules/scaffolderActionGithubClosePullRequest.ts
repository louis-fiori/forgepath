import { createBackendModule } from '@backstage/backend-plugin-api';
import {
  createTemplateAction,
  scaffolderActionsExtensionPoint,
} from '@backstage/plugin-scaffolder-node';
import { Octokit } from '@octokit/rest';

function parseRepoUrl(repoUrl: string): { owner: string; repo: string } {
  // Backstage scaffolder repoUrl format: "github.com?owner=X&repo=Y"
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

const scaffolderModuleGithubClosePullRequest = createBackendModule({
  pluginId: 'scaffolder',
  moduleId: 'github-close-pull-request',
  register({ registerInit }) {
    registerInit({
      deps: { scaffolder: scaffolderActionsExtensionPoint },
      async init({ scaffolder }) {
        scaffolder.addActions(
          createTemplateAction({
            id: 'github:closePullRequest',
            description:
              'Closes the open PR whose head branch matches branchName. Idempotent, logs a warning and exits cleanly if no PR is found.',
            schema: {
              input: {
                repoUrl: z =>
                  z
                    .string()
                    .describe(
                      'Backstage-format repo URL: github.com?owner=X&repo=Y',
                    ),
                branchName: z =>
                  z
                    .string()
                    .describe('Head branch of the PR to close.'),
              },
            },
            async handler(ctx) {
              const { repoUrl, branchName } = ctx.input;
              const { owner, repo } = parseRepoUrl(repoUrl);

              const token = process.env.GITHUB_TOKEN;
              if (!token) {
                throw new Error(
                  'GITHUB_TOKEN env var not set on the Backstage backend. ' +
                    'Apply the backstage-github-token.local.yaml secret and ' +
                    're-deploy Backstage so the env var is mounted.',
                );
              }

              const octokit = new Octokit({ auth: token });
              const { data: prs } = await octokit.pulls.list({
                owner,
                repo,
                state: 'open',
                head: `${owner}:${branchName}`,
              });

              if (prs.length === 0) {
                ctx.logger.warn(
                  `No open PR found with head branch "${branchName}" on ${owner}/${repo}. Nothing to close.`,
                );
                return;
              }

              for (const pr of prs) {
                ctx.logger.info(
                  `Closing PR #${pr.number}: ${pr.title}`,
                );
                await octokit.pulls.update({
                  owner,
                  repo,
                  pull_number: pr.number,
                  state: 'closed',
                });
              }
            },
          }),
        );
      },
    });
  },
});

export default scaffolderModuleGithubClosePullRequest;
