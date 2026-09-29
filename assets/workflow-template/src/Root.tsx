import React from 'react';
import {AbsoluteFill, Composition, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {Video} from '@remotion/media';

type Caption = {text: string; startMs: number; endMs: number};
type Scene = {title: string; start: number; end: number; purpose: string; card: string};
type Props = {
  source: string; title: string; duration: number; width: number; height: number; fps: number;
  captions: Caption[]; scenes: Scene[];
  visual: {template: 'documentary'|'editorial'; background: string; accent: string;
    textColor: string; subtitleSize: number; cardPosition: 'left'|'right'; titleCase: 'normal'|'bold'};
};

export const WorkflowVideo: React.FC<Props> = (props) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const second = frame / fps;
  const caption = props.captions.find((c) => c.startMs <= second * 1000 && c.endMs > second * 1000);
  const captionLines = caption ? caption.text.split('\n').length : 1;
  const subtitleSize = Math.round(Math.min(width,height)*props.visual.subtitleSize/1080);
  const scene = props.scenes.find((s) => s.card && second >= s.start && second < Math.min(s.end, s.start + 4));
  const editorial = props.visual.template === 'editorial';
  const portrait = height > width;
  const margin = Math.round(Math.min(width, height) * 0.065);
  const cardFrame = scene ? frame - Math.round(scene.start * fps) : 0;
  return <AbsoluteFill style={{backgroundColor: props.visual.background, color: props.visual.textColor, fontFamily: '"Noto Sans SC", "Microsoft YaHei", sans-serif'}}>
    <Video src={staticFile(props.source)} style={{width, height, objectFit: 'contain'}} />
    <AbsoluteFill style={{background: 'linear-gradient(0deg, rgba(0,0,0,0.68), transparent 30%)', pointerEvents: 'none'}} />
    <div style={{position: 'absolute', top: margin, left: margin, fontSize: Math.round(Math.min(width,height)*0.026), letterSpacing: 2, padding: '8px 12px', borderLeft: `4px solid ${props.visual.accent}`, color:'#FFFFFF', backgroundColor: 'rgba(0,0,0,0.6)'}}>{props.title}</div>
    {scene ? <div style={{position:'absolute', ...(portrait ? {bottom:height*0.18,left:margin,width:width-margin*2,boxSizing:'border-box' as const} : {top:height*(editorial ? 0.22 : 0.3),[props.visual.cardPosition]:margin}), maxWidth:portrait ? width-margin*2 : width*0.4, padding: `${margin * 0.55}px ${margin * 0.65}px`, borderTop: `5px solid ${props.visual.accent}`, backgroundColor: props.visual.background, opacity: interpolate(cardFrame, [0, 8], [0, 0.97], {extrapolateLeft:'clamp',extrapolateRight:'clamp'}), transform: `translateY(${interpolate(cardFrame,[0,10],[15,0],{extrapolateLeft:'clamp',extrapolateRight:'clamp'})}px)`, boxShadow: '0 12px 40px rgba(0,0,0,0.25)'}}>
      <div style={{fontSize: Math.round(Math.min(width,height)*0.025), color: props.visual.accent, marginBottom: 14}}>{scene.title}</div>
      <div style={{fontSize: Math.round(Math.min(width,height)*(portrait ? (scene.card.length>45 ? 0.038 : 0.045) : editorial ? 0.055 : 0.048)), lineHeight:1.35, fontWeight: props.visual.titleCase === 'bold' ? 800 : 600, whiteSpace:'pre-wrap', textWrap:'balance'}}>{scene.card}</div>
    </div> : null}
    {caption ? <div style={{position:'absolute', left:margin, right:margin, bottom:margin, display:'flex', justifyContent:'center'}}>
      <div style={{fontSize:Math.max(Math.round(Math.min(width,height)*32/1080),Math.round(subtitleSize*Math.min(1,2/captionLines))), fontWeight:650, lineHeight:1.45, textAlign:'center', whiteSpace:'pre-wrap', maxWidth:'100%', padding:'10px 22px', borderRadius:8, color:'#FFFFFF', backgroundColor:'rgba(0,0,0,0.76)', textShadow:'0 2px 4px #000'}}>{caption.text}</div>
    </div> : null}
  </AbsoluteFill>;
};

const defaults: Props = {source:'source.mp4', title:'', duration:1, width:1920, height:1080, fps:30, captions:[], scenes:[], visual:{template:'documentary',background:'#182020',accent:'#D1B479',textColor:'#F5F1E8',subtitleSize:48,cardPosition:'left',titleCase:'bold'}};
export const Root: React.FC = () => <Composition id="WorkflowVideo" component={WorkflowVideo} durationInFrames={30} fps={30} width={1920} height={1080} defaultProps={defaults} calculateMetadata={({props}) => ({durationInFrames:Math.max(1,Math.ceil(props.duration*props.fps)),fps:props.fps,width:props.width,height:props.height})} />;
