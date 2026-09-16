declare module "p5/core" { import p5 from "p5"; export default p5; }
declare module "p5/color" { import p5 from "p5"; const addon: (constructor: typeof p5) => void; export default addon; }
declare module "p5/shape" { import p5 from "p5"; const addon: (constructor: typeof p5) => void; export default addon; }
declare module "p5/math" { import p5 from "p5"; const addon: (constructor: typeof p5) => void; export default addon; }
declare module "p5/type" { import p5 from "p5"; const addon: (constructor: typeof p5) => void; export default addon; }

declare module "p5/accessibility" { import p5 from "p5"; const addon: (constructor: typeof p5) => void; export default addon; }
