import { createBackendModule } from '@backstage/backend-plugin-api';
import {
  createTemplateAction,
  scaffolderActionsExtensionPoint,
} from '@backstage/plugin-scaffolder-node';
import { KubeConfig, KubernetesObjectApi } from '@kubernetes/client-node';
import { readFile, readdir } from 'fs/promises';
import { resolve as resolvePath } from 'path';
import { loadAll } from 'js-yaml';

const scaffolderModuleKubernetesApply = createBackendModule({
  pluginId: 'scaffolder',
  moduleId: 'kubernetes-apply',
  register({ registerInit }) {
    registerInit({
      deps: { scaffolder: scaffolderActionsExtensionPoint },
      async init({ scaffolder }) {
        scaffolder.addActions(
          createTemplateAction({
            id: 'kubernetes:apply',
            description:
              'Applies every YAML file under `manifestsDir` to the cluster the backend is running in (in-cluster kubeconfig).',
            schema: {
              input: {
                manifestsDir: z =>
                  z
                    .string()
                    .describe(
                      'Directory (relative to the scaffolder workspace) containing the YAML files to apply.',
                    ),
              },
            },
            async handler(ctx) {
              const dir = resolvePath(ctx.workspacePath, ctx.input.manifestsDir);
              const kc = new KubeConfig();
              kc.loadFromDefault();
              const client = KubernetesObjectApi.makeApiClient(kc);

              const files = (await readdir(dir)).filter(f =>
                /\.ya?ml$/i.test(f),
              );

              // Collect every doc from every file, then sort so that things
              // other resources depend on get applied first. Without this,
              // a Deployment can be applied before its Namespace exists.
              const docs: Record<string, unknown>[] = [];
              for (const file of files) {
                const content = await readFile(resolvePath(dir, file), 'utf8');
                docs.push(
                  ...(loadAll(content) as unknown[]).filter(
                    (d): d is Record<string, unknown> =>
                      d != null && typeof d === 'object',
                  ),
                );
              }

              const kindOrder: Record<string, number> = {
                Namespace: 0,
                CustomResourceDefinition: 1,
                ServiceAccount: 2,
                ConfigMap: 2,
                Secret: 2,
                ClusterRole: 3,
                Role: 3,
                ClusterRoleBinding: 4,
                RoleBinding: 4,
              };
              const priority = (o: Record<string, unknown>) =>
                kindOrder[(o as any).kind] ?? 10;
              docs.sort((a, b) => priority(a) - priority(b));

              // Use Server-Side Apply (the native `kubectl apply` equivalent):
              // idempotent on every call, handles already-allocated fields
              // (e.g. NodePort reuse), and tracks field ownership via the
              // `fieldManager` identifier. `force: true` overrides conflicts.
              for (const obj of docs) {
                const kind = (obj as any).kind;
                const name = (obj as any).metadata?.name;
                ctx.logger.info(`Applying ${kind}/${name}`);

                await client.patch(
                  obj as any,
                  undefined, // pretty
                  undefined, // dryRun
                  'forgepath-scaffolder', // fieldManager (SSA owner identity)
                  true, // force — overwrite fields owned by other managers
                  {
                    headers: {
                      'Content-Type': 'application/apply-patch+yaml',
                    },
                  } as any,
                );
              }
            },
          }),
        );
      },
    });
  },
});

export default scaffolderModuleKubernetesApply;
