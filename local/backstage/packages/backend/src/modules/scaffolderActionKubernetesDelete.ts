import { createBackendModule } from '@backstage/backend-plugin-api';
import {
  createTemplateAction,
  scaffolderActionsExtensionPoint,
} from '@backstage/plugin-scaffolder-node';
import { KubeConfig, CoreV1Api } from '@kubernetes/client-node';

// Namespaces this action will refuse to delete. Either k8s system namespaces
// or platform components — wiping any of these would brick the cluster or the
// observability/GitOps stack.
const PROTECTED_NAMESPACES = new Set([
  'kube-system',
  'kube-public',
  'kube-node-lease',
  'default',
  'local-path-storage',
  'backstage',
  'argocd',
  'prometheus',
  'grafana',
]);

const scaffolderModuleKubernetesDelete = createBackendModule({
  pluginId: 'scaffolder',
  moduleId: 'kubernetes-delete',
  register({ registerInit }) {
    registerInit({
      deps: { scaffolder: scaffolderActionsExtensionPoint },
      async init({ scaffolder }) {
        scaffolder.addActions(
          createTemplateAction({
            id: 'kubernetes:delete',
            description:
              'Deletes a Kubernetes namespace (cascades to all resources inside). Refuses to delete system or platform namespaces.',
            schema: {
              input: {
                namespace: z =>
                  z
                    .string()
                    .describe('Name of the namespace to delete.'),
              },
            },
            async handler(ctx) {
              const { namespace } = ctx.input;

              if (PROTECTED_NAMESPACES.has(namespace)) {
                throw new Error(
                  `Refusing to delete protected namespace "${namespace}". ` +
                    `Protected list: ${[...PROTECTED_NAMESPACES].join(', ')}.`,
                );
              }

              const kc = new KubeConfig();
              kc.loadFromDefault();
              const client = kc.makeApiClient(CoreV1Api);

              ctx.logger.info(`Deleting namespace ${namespace}`);
              try {
                await client.deleteNamespace(namespace);
              } catch (err: any) {
                const statusCode =
                  err?.statusCode ?? err?.response?.statusCode;
                if (statusCode === 404) {
                  ctx.logger.warn(
                    `Namespace ${namespace} not found (already deleted or never existed).`,
                  );
                  return;
                }
                throw err;
              }
            },
          }),
        );
      },
    });
  },
});

export default scaffolderModuleKubernetesDelete;
