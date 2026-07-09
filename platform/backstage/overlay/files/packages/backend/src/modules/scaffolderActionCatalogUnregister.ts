import { createBackendModule } from '@backstage/backend-plugin-api';
import { catalogServiceRef } from '@backstage/plugin-catalog-node';
import {
  createTemplateAction,
  scaffolderActionsExtensionPoint,
} from '@backstage/plugin-scaffolder-node';

const scaffolderModuleCatalogUnregister = createBackendModule({
  pluginId: 'scaffolder',
  moduleId: 'catalog-unregister',
  register({ registerInit }) {
    registerInit({
      deps: {
        scaffolder: scaffolderActionsExtensionPoint,
        catalog: catalogServiceRef,
      },
      async init({ scaffolder, catalog }) {
        scaffolder.addActions(
          createTemplateAction({
            id: 'catalog:unregister',
            description:
              'Inverse of catalog:register: removes the Location registered for a catalog-info URL and every entity it emitted. Idempotent, logs a warning and exits cleanly if nothing is registered.',
            schema: {
              input: {
                catalogInfoUrl: z =>
                  z
                    .string()
                    .describe(
                      'The catalog-info.yaml URL previously passed to catalog:register.',
                    ),
              },
            },
            async handler(ctx) {
              const { catalogInfoUrl } = ctx.input;
              const credentials = await ctx.getInitiatorCredentials();
              const locationRef = `url:${catalogInfoUrl}`;

              // Remove the Location first so the entities deleted below don't
              // get re-emitted on the next catalog refresh.
              const location = await catalog.getLocationByRef(locationRef, {
                credentials,
              });
              if (location) {
                await catalog.removeLocationById(location.id, { credentials });
                ctx.logger.info(`Removed catalog location ${locationRef}`);
              } else {
                ctx.logger.warn(
                  `No catalog location found for ${locationRef}. Nothing to unregister.`,
                );
              }

              // The default orphanStrategy is `keep`, so the emitted entities
              // would linger as orphans, delete them explicitly.
              const { items } = await catalog.getEntities(
                {
                  filter: {
                    'metadata.annotations.backstage.io/managed-by-origin-location':
                      locationRef,
                  },
                },
                { credentials },
              );
              for (const entity of items) {
                if (!entity.metadata.uid) {
                  continue;
                }
                await catalog.removeEntityByUid(entity.metadata.uid, {
                  credentials,
                });
                ctx.logger.info(
                  `Deleted entity ${entity.kind}:${
                    entity.metadata.namespace ?? 'default'
                  }/${entity.metadata.name}`,
                );
              }
            },
          }),
        );
      },
    });
  },
});

export default scaffolderModuleCatalogUnregister;
