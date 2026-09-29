# Fixed workflow video template

This template contains no personal footage. Each workflow receives its own
`src`, `public`, `props.json` and dependency provenance. It reuses the existing
Remotion 4.0.503 installation without installing packages.

Supported: continuous original A-roll with its audio, timestamped captions,
few chapter cards, 16:9 / 9:16 / 1:1 canvas, documentary/editorial preset,
validated colors, caption size, card side and title weight.

Not implemented by changing a Skill: arbitrary new layouts, new components,
B-roll placement, rough cutting, extra music, 3D and custom visual effects.
The model lists unsupported requests in `build-report.json`; code extensions
are separate work. Inputs do not contain model-generated executable code.

Timeline JSON uses seconds and must cover the original duration continuously:

```json
{"scenes":[{"title":"Chapter","start":0,"end":12,"purpose":"Explain the idea","card":"One key point"}]}
```

Captions use Remotion millisecond fields `text`, `startMs`, `endMs`,
`timestampMs`, `confidence`. No estimated timestamps are generated from text.
Review includes a short preview and boundary stills. Human confirmation is
handled by the workflow service before full delivery.
