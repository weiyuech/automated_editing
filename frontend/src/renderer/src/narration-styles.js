// Stable order is also the automatic batch assignment order.
export const narrationStyles = [
  { id: 'natural', name: '自然讲解', description: '清楚、亲切、具体，像熟悉内容的人带您参观。', rules: "顺着资料用口语解释，完整短句，少用介绍套话。" },
  { id: 'professional', name: '专业解析', description: '解释用途、特点和原理，逻辑清晰。', rules: "先对象，再规格、功能；用准确名词与限定条件，不给没有依据的性能结论。" },
  { id: 'concise', name: '简洁有力', description: '短句、直接、重点突出，少铺垫和修饰。', rules: "删铺垫，用短句报重点；可以少选事实，但不能把“后排座椅按比例放倒”改成“座椅放倒”。" },
  { id: 'humorous', name: '轻松幽默', description: '俏皮、有趣，用适度反差增加趣味。', rules: "轻巧的口语反差或自问自答，不固定开场，不吹效果。例如原文“有红色和蓝色”，可写“颜色不用猜，红色、蓝色，两种选择。”示例只是句式。" },
  { id: 'poetic', name: '诗意抒情', description: '现代中文，有意境和韵律，不堆砌辞藻。', rules: "用短对句的节奏，少用形容词。例如原文“有红茶和绿茶”，可写“两款茶，两种选择：红茶，绿茶。”不能添加香味、景色、心情或功效。示例只是句式。" },
  { id: 'classical', name: '文言雅述', description: '文白相间，简练典雅，容易听懂。', rules: "简洁文白相间，句法通顺。例如“有红色和蓝色”可写“有红，亦有蓝。”保留现代型号、数字、单位，不换算古代时辰。示例只是句式。" },
]
export function narrationStyle(id, index = 0) {
  return id === 'auto' ? narrationStyles[index % narrationStyles.length] : narrationStyles.find(style => style.id === id) || narrationStyles[0]
}
