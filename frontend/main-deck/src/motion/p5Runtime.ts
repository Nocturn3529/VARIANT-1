import P5 from "p5/core";
import accessibility from "p5/accessibility";
import color from "p5/color";
import math from "p5/math";
import shape from "p5/shape";
import typography from "p5/type";

// Instance-mode 2D drawing only. Do not load the global sketch initializer or
// developer source/parameter evaluators into the app's strict script policy.
for (const addon of [accessibility, color, math, shape, typography]) P5.registerAddon(addon);
P5.disableFriendlyErrors = true;
export default P5;
