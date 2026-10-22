async ({taskId, clientId}) => {
  // 公开版不包含"在得到官网网页里自动上传"的脚本：它依赖官网前端的内部模块，
  // 官网一改版就会失效，也可能不符合服务条款。browser.py 会在专用浏览器页面里
  // 执行这个函数；需要接入转写服务时，请改用服务的官方上传接口（如果有），
  // 或在这里实现你自己的上传逻辑。返回 {failure: '<code>'} 表示失败。
  return {failure: 'upload_not_included'};
}
