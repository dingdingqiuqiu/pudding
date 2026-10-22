# 简历排版脚本
# ============
# 用 PowerPoint 实测文本框真实高度，再把「专业技能 / 项目经历 / 证书奖项」三节
# 按顺序重新摆放，并把「布丁智服」的项目地址文本框对齐到它的标题行，
# 保证单页 A4 不溢出。
#
# 用法：
#   pwsh -File tools/resume_layout.ps1 -Mode report     # 只报告实测高度
#   pwsh -File tools/resume_layout.ps1 -Mode layout     # 重新排版并导出预览图
#
# 注：系统执行策略禁止直接运行 .ps1，可用
#   $sb = [scriptblock]::Create((Get-Content -Raw -Encoding UTF8 tools/resume_layout.ps1)); & $sb -Mode layout

param(
  [ValidateSet('report', 'layout')]
  [string]$Mode = 'report',
  [string]$Pptx = 'D:\1devCode\EchoMind所有代码+详细文档+简历\布丁智服\简历\张丁-简历-布丁智服版.pptx',
  [string]$Png  = 'D:\1devCode\EchoMind所有代码+详细文档+简历\布丁智服\简历\预览.png'
)

$ErrorActionPreference = 'Stop'
$PAGE_H = 780.0        # A4 竖版高度（pt）
$BODY_OFFSET = 18.1    # 小节标题 -> 正文顶部的距离（pt）
$SEC_GAP = 12.0        # 上一节正文底部 -> 下一节标题的距离（pt）
$BOTTOM_MARGIN = 14.0  # 页面底部留白（pt）
$NEW_TITLE_INDEX = 14  # 「项目经历」正文里，布丁智服标题是第 14 段

function Find-ShapeByText($slide, [string[]]$candidates) {
  foreach ($shape in $slide.Shapes) {
    try { $t = $shape.TextFrame.TextRange.Text } catch { continue }
    if (-not $t) { continue }
    foreach ($c in $candidates) { if ($t.Contains($c)) { return $shape } }
  }
  return $null
}

$pp = New-Object -ComObject PowerPoint.Application
$pres = $pp.Presentations.Open($Pptx)
$slide = $pres.Slides.Item(1)

$secTitle = @{
  skills   = Find-ShapeByText $slide @('专业技能')
  projects = Find-ShapeByText $slide @('项目经历')
  awards   = Find-ShapeByText $slide @('证书奖项')
}
$bodies = @{
  skills   = Find-ShapeByText $slide @('网络基础')
  projects = Find-ShapeByText $slide @('听风生活志')
  awards   = Find-ShapeByText $slide @('英语：')
}

Write-Output "=== 实测 ==="
foreach ($k in @('skills', 'projects', 'awards')) {
  $b = $bodies[$k]; $t = $secTitle[$k]
  if (-not $b -or -not $t) { Write-Output "$k : 未找到形状"; continue }
  $h = $b.TextFrame2.TextRange.BoundHeight
  Write-Output ("{0,-9} title.Top={1,7:N1} body.Top={2,7:N1} BoundHeight={3,7:N1} body.Bottom={4,7:N1}" -f `
      $k, $t.Top, $b.Top, $h, ($b.Top + $h))
}

if ($Mode -eq 'layout') {
  foreach ($k in @('skills', 'projects', 'awards')) {
    $bodies[$k].TextFrame2.AutoSize = 0   # msoAutoSizeNone
  }

  # 第一小节（专业技能）位置不动，之后逐节往下推
  $cursor = $bodies['skills'].Top + $bodies['skills'].TextFrame2.TextRange.BoundHeight + $SEC_GAP
  foreach ($k in @('projects', 'awards')) {
    $secTitle[$k].Top = $cursor
    $bodies[$k].Top = $cursor + $BODY_OFFSET
    $cursor = $bodies[$k].Top + $bodies[$k].TextFrame2.TextRange.BoundHeight + $SEC_GAP
  }

  # 两个「项目地址」文本框：垂直位置跟各自的标题行保持一致
  # 做法：先量出「听风」链接框里文字相对框顶的偏移，再反推每个标题行该放哪里
  $linkTpl = Find-ShapeByText $slide @('tfshz')
  $linkNew = Find-ShapeByText $slide @('dingdingqiuqiu/pudding')
  $paras = $bodies['projects'].TextFrame.TextRange
  if ($linkTpl -and $linkNew) {
    # 原始设计里，链接框里的文字比标题段落顶部低 5.3pt（从改造前的文件实测）
    $LINK_TEXT_DY = 5.3
    $inset = $linkTpl.TextFrame2.TextRange.BoundTop - $linkTpl.Top
    $para1 = $paras.Paragraphs(1, 1)
    $paraN = $paras.Paragraphs($NEW_TITLE_INDEX, 1)
    $linkTpl.Top = $para1.BoundTop + $LINK_TEXT_DY - $inset
    $linkNew.Top = $paraN.BoundTop + $LINK_TEXT_DY - $inset
    Write-Output ("链接框 Top：听风 {0:N1} / 布丁智服 {1:N1}（文字内偏移 {2:N1}）" -f `
        $linkTpl.Top, $linkNew.Top, $inset)
  } else {
    Write-Output '!! 没找到项目地址文本框'
  }

  $lastBottom = $bodies['awards'].Top + $bodies['awards'].TextFrame2.TextRange.BoundHeight
  Write-Output ("排版后底部 = {0:N1} pt（页面 {1}，可用上限 {2:N1}）" -f $lastBottom, $PAGE_H, ($PAGE_H - $BOTTOM_MARGIN))
  if ($lastBottom -gt $PAGE_H - $BOTTOM_MARGIN) { Write-Output '!! 超出页面，需要压缩内容或行距' }

  $pres.Save()
  $slide.Export($Png, 'PNG', 1600, 1600 * 780 / 540)
  Write-Output "预览图：$Png"
}

$pres.Close()
$pp.Quit()
