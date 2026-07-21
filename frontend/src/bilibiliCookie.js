export const BILIBILI_COOKIE_FIELDS = [
  "SESSDATA",
  "bili_jct",
  "DedeUserID",
  "DedeUserID__ckMd5",
  "bili_ticket",
  "bili_ticket_expires",
];

const BILIBILI_COOKIE_FIELD_SET = new Set(BILIBILI_COOKIE_FIELDS);

export function simplifyBilibiliCookieHeader(rawCookie = "") {
  let text = String(rawCookie || "").trim();
  if (text.toLowerCase().startsWith("cookie:")) {
    text = text.slice(text.indexOf(":") + 1).trim();
  }

  const values = new Map();
  for (const part of text.split(";")) {
    const index = part.indexOf("=");
    if (index < 0) {
      continue;
    }
    const key = part.slice(0, index).trim();
    const value = part.slice(index + 1).trim();
    if (!key || !value || !BILIBILI_COOKIE_FIELD_SET.has(key)) {
      continue;
    }
    values.set(key, value);
  }

  return BILIBILI_COOKIE_FIELDS.filter((field) => values.has(field))
    .map((field) => `${field}=${values.get(field)}`)
    .join("; ");
}

export function summarizeBilibiliCookie(rawCookie = "") {
  const cookieHeader = simplifyBilibiliCookieHeader(rawCookie);
  const present = new Set();
  for (const part of cookieHeader.split(";")) {
    const index = part.indexOf("=");
    if (index < 0) {
      continue;
    }
    const key = part.slice(0, index).trim();
    if (BILIBILI_COOKIE_FIELD_SET.has(key)) {
      present.add(key);
    }
  }

  return {
    cookieHeader,
    fields: BILIBILI_COOKIE_FIELDS.filter((field) => present.has(field)),
    missingFields: BILIBILI_COOKIE_FIELDS.filter((field) => !present.has(field)),
  };
}
