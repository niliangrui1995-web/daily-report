// ============================================================
// Cloudflare Worker：Google Gemini API 反向代理
// 功能：将发往您专属域名的请求，在海外机房内部转发给 Google 官方 API
// 部署位置：Cloudflare Dashboard → Workers & Pages → 创建 Worker
// 绑定域名：api.niliangrui.cloud
// ============================================================

// 目标：Google Gemini API 官方地址
const GOOGLE_API_HOST = "generativelanguage.googleapis.com";

export default {
  async fetch(request) {
    // 获取原始请求的 URL 并替换 host 为 Google 官方
    const url = new URL(request.url);
    url.hostname = GOOGLE_API_HOST;
    url.protocol = "https:";

    // 克隆原始请求的所有内容（method、headers、body）
    const newRequest = new Request(url.toString(), {
      method: request.method,
      headers: request.headers,
      body: request.body,
      redirect: "follow",
    });

    // 在 Cloudflare 海外节点发起对 Google 的请求
    const response = await fetch(newRequest);

    // 将 Google 的响应原封不动返回给调用者，并添加 CORS 头
    const newResponse = new Response(response.body, response);
    newResponse.headers.set("Access-Control-Allow-Origin", "*");
    newResponse.headers.set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
    newResponse.headers.set("Access-Control-Allow-Headers", "Content-Type, Authorization, x-goog-api-key");

    return newResponse;
  },
};
