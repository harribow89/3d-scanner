# Scanner Feature Plan

This plan follows the Agency workflow in a lightweight way:

- Agents Orchestrator: keep work grouped into planning, implementation, and validation lanes.
- Senior Project Manager: define concrete, testable scanner improvements instead of vague polish.
- Senior Developer: ship the highest-value features directly in the app surface the user actually runs.

## Implemented In This Pass

- Multi-strategy point cloud isolation after every export.
- Recommended isolated cloud selection plus saved comparison variants.
- Re-isolate and preview controls from the main app.
- Live scan guidance text based on RTAB-Map telemetry.
- Expanded AI control over isolation profile, depth window, auto-export, re-isolation, and preview.

## Feature Lanes

1. Export quality lane
   Add post-export cleanup, tabletop segmentation, center-focus cropping, and aggressive hybrid cleanup.
2. Live guidance lane
   Convert odometry and loop-closure telemetry into immediate scan advice inside the UI.
3. AI control lane
   Let the agent change export/isolation settings and trigger follow-up actions instead of only narrating.
4. Operator workflow lane
   Keep latest raw and recommended isolated cloud easy to preview and reprocess.

## Next Additions

1. Mesh reconstruction from isolated clouds.
2. Export-side mesh decimation presets for printing vs archive quality.
3. Auto-score isolated variants by compactness, vertical support removal, and point density.
4. Guided scan checklist with top/side/bottom coverage progress.