/* Pure placement geometry shared by the preview editor and browserless tests. */
(function(root){
'use strict';
const WIDTH=1920,HEIGHT=1080;
const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));

function clientToLogical(rect,clientX,clientY){
  const width=Math.max(1,Number(rect?.width)||0),height=Math.max(1,Number(rect?.height)||0);
  return{
    x:(Number(clientX)-Number(rect?.left||0))*WIDTH/width,
    y:(Number(clientY)-Number(rect?.top||0))*HEIGHT/height,
  };
}

function hitTest(rows,point,selectedKind,handleSize=24){
  const list=Array.isArray(rows)?rows:[];
  const selected=list.find(row=>row[0]===selectedKind);
  if(selected){
    const box=selected[2]||{},size=Math.max(1,Number(handleSize)||24);
    if(point.x>=box.x+box.w-size&&point.x<=box.x+box.w+size&&
       point.y>=box.y+box.h-size&&point.y<=box.y+box.h+size){
      return{kind:selected[0],mode:'resize',box};
    }
  }
  for(let index=list.length-1;index>=0;index--){
    const row=list[index],box=row[2]||{};
    if(point.x>=box.x&&point.x<=box.x+box.w&&point.y>=box.y&&point.y<=box.y+box.h){
      return{kind:row[0],mode:'move',box};
    }
  }
  return null;
}

function moveBox(box,start,current){
  return{
    x:clamp(Number(box.x)+(current.x-start.x),0,Math.max(0,WIDTH-Number(box.w))),
    y:clamp(Number(box.y)+(current.y-start.y),0,Math.max(0,HEIGHT-Number(box.h))),
    w:Number(box.w),h:Number(box.h),
  };
}

function resizeBox(box,start,current,minWidth=1,minHeight=1){
  return{
    x:Number(box.x),y:Number(box.y),
    w:clamp(Number(box.w)+(current.x-start.x),Number(minWidth),Math.max(Number(minWidth),WIDTH-Number(box.x))),
    h:clamp(Number(box.h)+(current.y-start.y),Number(minHeight),Math.max(Number(minHeight),HEIGHT-Number(box.y))),
  };
}

function alignBox(box,axis,value,margin=60){
  const result={x:Number(box.x)||0,y:Number(box.y)||0,w:Number(box.w)||1,h:Number(box.h)||1};
  const gap=Math.max(0,Number(margin)||0);
  if(axis==='x')result.x=value==='left'?gap:value==='right'?WIDTH-result.w-gap:(WIDTH-result.w)/2;
  else result.y=value==='top'?gap:value==='bottom'?HEIGHT-result.h-gap:(HEIGHT-result.h)/2;
  result.x=clamp(result.x,0,Math.max(0,WIDTH-result.w));
  result.y=clamp(result.y,0,Math.max(0,HEIGHT-result.h));
  return result;
}

function savePlacement(commit,before){
  if(typeof commit!=='function')throw new TypeError('placement commit callback is required');
  return commit(before);
}

root.AssemblyPlacement={WIDTH,HEIGHT,clientToLogical,hitTest,moveBox,resizeBox,alignBox,savePlacement};
})(typeof window!=='undefined'?window:globalThis);
