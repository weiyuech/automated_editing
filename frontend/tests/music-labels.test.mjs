import test from 'node:test'
import assert from 'node:assert/strict'
import { groupMusic, isAudioFile } from '../src/renderer/src/music-labels.js'

test('groups share original IDs: multi-label music never becomes a second selectable asset', () => {
  const a={id:'a',metadata:{music_labels:['国风','舒缓']}}
  const b={id:'b',metadata:{music_labels:[]}}
  const c={id:'c',metadata:{music_labels:['轻快','轻快']}}
  const groups=groupMusic([a,b,c])
  assert.deepEqual(groups.map(g=>g.label),['轻快','舒缓','国风','未分类'])
  assert.equal(groups[1].items[0],a)
  assert.equal(groups[2].items[0],a)
  assert.deepEqual(groups[0].items,[c])
  assert.deepEqual(groups[3].items,[b])
})
test('legacy music is unclassified, file recognition ignores extension case',()=>{
  assert.equal(groupMusic([{id:'old'}])[0].label,'未分类')
  assert.equal(isAudioFile('C:\\Music\\track.MP3'),true)
  assert.equal(isAudioFile('/video.mp4'),false)
  assert.deepEqual(groupMusic([]),[])
})
