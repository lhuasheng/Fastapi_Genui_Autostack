/**
 * Merge the four pipeline artifacts into the single manifest the A2UI agent loads.
 * Pure function (no I/O) so it can be unit-tested directly.
 */
export function buildManifest({ tools, catalog, apiGraph, mapping, source }) {
  const dataShapes = {};
  for (const node of apiGraph.nodes || []) {
    for (const op of node.operations || []) {
      if (op.data_shape) dataShapes[op.slug] = op.data_shape;
    }
  }

  const provenance = {};
  if (apiGraph.probe?.stats) provenance.probe_stats = apiGraph.probe.stats;
  if (catalog.collisions) provenance.collisions = catalog.collisions;
  if (catalog.parseErrors) provenance.parseErrors = catalog.parseErrors;

  return {
    version: "1.0",
    generated: new Date().toISOString(),
    ...(source && { source }),
    tools: tools || [],
    a2ui: {
      ...(catalog.renderTool && { renderTool: catalog.renderTool }),
      blocks: catalog.blocks || {},
      ...(catalog.blockSchema && { blockSchema: catalog.blockSchema }),
      ...(catalog.composition && { composition: catalog.composition }),
    },
    dataShapes,
    mapping,
    provenance,
  };
}
