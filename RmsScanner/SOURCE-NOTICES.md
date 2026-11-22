# Source and model notices

The RMS scanner imported the following from `curve-chart-beauty` on 2026-11-04: pipeline orchestration, production
Python stage implementations (`image-preprocess`, `moire-remove`,
`chart-enhance`), production tests, dependency manifests/locks and CPU installer.
The original import was authorized for the RMS application. ExternalImages packages the scanner implementation. Supply the checkpoint separately at deployment.

Adaptations: HTTP compatibility adapter retained, fixed CPU/2048/chart pipeline,
offline no-sync subprocess invocation, CPU thread controls, production-only CLI
entry points/dependencies, CPU torch lock. Training and Web UIs are not included.
The inference algorithms remain from the source project.

Model reference: MBCNN authors' repository:
https://github.com/zhenngbolun/Learnbale_Bandpass_Filter

Upstream warning retained: public code and accessible weights do **not** imply
unrestricted redistribution authorization; independently confirm the permitted
use/distribution scope. This import is based on the user's explicit authorization,
not a claim that the model has a new unrestricted license. No additional upstream
license files were present in the imported production source directories. Do not
remove existing author notices in source code.

Selected checkpoint (not source default `weights/step-20000.h5`):
`moire-remove/runs/input3-original-10000-20261103-1702/step-010000.h5`
→ `models/default.h5`.

SHA-256: `d956874fd238b6c40336b7358990ca576ef4272b8f5b15a10a1e5cfcd8265781`.

Do not infer a general license for other source assets or checkpoints. No original
photographs, source training data, alternate weights or training run directories
are included. Nothing downloads or trains weights automatically.
