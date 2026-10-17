# Third-party notices

AgentBodycam (MIT) bundles a production build of the Replay UI. That build includes
runtime code from the following third-party packages. This file is the authoritative
attribution for that bundled code; do not rely on license comments inside minified
JavaScript.

AgentBodycam's own license remains MIT ([LICENSE](LICENSE)). The packages below are not
licensed as AgentBodycam's own work; their upstream copyright notices and license text
are reproduced here as required.

Versions below match the packages used when the committed Replay UI build was produced
(`web/package-lock.json`).

## Bundled runtime packages

The following packages contribute code that is distributed in
`agentbodycam/ui/static/` (the Replay UI wheel contents):

| Package | Upstream version | License |
|---|---|---|
| [react](https://www.npmjs.com/package/react) | 19.3.0 | MIT |
| [react-dom](https://www.npmjs.com/package/react-dom) | 19.3.0 | MIT |
| [scheduler](https://www.npmjs.com/package/scheduler) | 0.28.0 | MIT |

`scheduler` is a runtime dependency of `react-dom` and is bundled into the same
production JavaScript assets.

Dev-only tools used to build the UI (Vite, TypeScript, Vitest, Testing Library, type
packages, and similar) are **not** shipped in the wheel and are omitted here.

## Shared MIT license text

`react`, `react-dom`, and `scheduler` ship identical MIT license text. Copyright and
permission notice (verbatim from each package's `LICENSE` file):

```text
MIT License

Copyright (c) Meta Platforms, Inc. and affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
