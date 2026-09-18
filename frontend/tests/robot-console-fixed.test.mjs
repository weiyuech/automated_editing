import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

function consoleFixture() {
  const elements = new Map(), timers = []
  let now = 1000
  function element(id) {
    if (!elements.has(id)) elements.set(id, { value:'', disabled:false, textContent:'', innerHTML:'', classList:{toggle(){}}, setAttribute(){}, appendChild(){} })
    return elements.get(id)
  }
  for (const [id,value] of Object.entries({yawMin:-10,yawMax:30,pitchMin:-20,pitchMax:5,speedMax:5,pointMode:4})) element(id).value = String(value)
  const context = vm.createContext({ document: { getElementById:element, querySelectorAll:()=>[], createElement:()=>element(Symbol()), createTextNode:x=>x }, Date: {now:()=>now}, window:{setInterval(){},setTimeout:fn=>timers.push(fn),clearTimeout(){},localStorage:{getItem:()=>null}} })
  const html = readFileSync(new URL('../../tools/robot-control-console.html',import.meta.url),'utf8')
  const script = html.split('<script>')[1].split('</script>')[0].replace('})();', 'globalThis.fixture = {fixedPieces,waitForPose,open:()=>{ws={readyState:1}}};})();')
  vm.runInContext(script,context)
  return {fixture:context.fixture,element,advance(){now+=10000;timers.splice(0).forEach(fn=>fn())}}
}

test('standalone fixed program has the same physical directions and corner round trips', () => {
  const {fixture,element}=consoleFixture()
  const pieces=JSON.parse(JSON.stringify(fixture.fixedPieces()))
  assert.deepEqual(pieces.map(p=>p.poses), [[[0,0],[30,0]],[[30,0],[-10,0]],[[-10,0],[0,0]],[[0,0],[0,-20]],[[0,-20],[0,5]],[[0,5],[0,0]]])
  element('pointMode').value='8'
  assert.deepEqual(JSON.parse(JSON.stringify(fixture.fixedPieces())).slice(6).map(p=>p.poses), [[[0,0],[30,-20],[0,0]],[[0,0],[-10,-20],[0,0]],[[0,0],[-10,5],[0,0]],[[0,0],[30,5],[0,0]]])
})

test('standalone elapsed time without new feedback never becomes arrival', async () => {
  const {fixture,advance}=consoleFixture()
  fixture.open()
  const waiting=fixture.waitForPose(0,0,1,()=>false,{yaw:0,pitch:0})
  advance()
  assert.equal(await waiting,'timeout')
})
