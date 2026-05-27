// Thin fetch wrapper for the FastAPI backend.
// every exported function maps to one endpoint.

import { STORAGE_KEYS } from "@/utils/constants";

const API_BASE = (process.env.NEXT_PUBLIC_API_BASE || "").trim();

const authHeader = () => {
  if (typeof window === "undefined") return {};
  const t = window.localStorage.getItem(STORAGE_KEYS.token);
  return t ? { Authorization: `Bearer ${t}` } : {};
};

// network errors get their own flag so UI can show a friendly "API unreachable".
const isNetworkError = (err) =>
  err?.name === "TypeError" ||
  /failed to fetch|networkerror|load failed|abort/i.test(String(err?.message || ""));

const request = async (path, { method = "GET", body, headers, timeoutMs = 60_000 } = {}) => {
  
  if (!API_BASE) {
    const err = new Error(
      "NEXT_PUBLIC_API_BASE is not set. Point it to your FastAPI server (e.g. http://127.0.0.1:8000).",
    );
    err.status = 0;
    throw err;
  }

  let attempt =0;
  const maxRetries =1 ;

  while(true){

  const ac = typeof AbortController !== "undefined" ? new AbortController() : null;
  const to = ac ? setTimeout(() => ac.abort(), timeoutMs) : null;
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: { "Content-Type": "application/json", ...authHeader(), ...headers },
      body: body ? JSON.stringify(body) : undefined,
      signal: ac?.signal,
    });
    if (!res.ok) {
      // try to pull a readable reason out of the body, fall back to status.
      let msg = `Request failed (${res.status})`;
      try {
        const data = await res.json();
        const firstValErr = Array.isArray(data?.detail) ? data.detail[0] : null;
        const detailText =
          typeof data?.detail === "string"
            ? data.detail
            : firstValErr && typeof firstValErr === "object" && (firstValErr.msg || firstValErr.message)
              ? firstValErr.msg || firstValErr.message
              : null;
        msg = data?.message || detailText || msg;
      } catch {
        
        /* ignore body parse errors */
      }
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return res.json();
  } catch (err) {
    const timedOut = err?.name === "AbortError";
    if (isNetworkError(err) || timedOut) {
      const nicer = new Error(
        `Can't reach the API at ${API_BASE}. Make sure the FastAPI server is up and CORS_ORIGINS includes this site.`,
      );
      nicer.cause = err;
      nicer.isNetwork = true;
      throw nicer;
    }
    throw err;
  } finally {
    if (to) clearTimeout(to);
  }
  }
};

// ------------------------------ auth ------------------------------

export const sendOtp = ({ email }) =>
  request("/send-otp", { method: "POST", body: { email } });

export const verifyOtp = ({ email, otp }) =>
  request("/verify-otp", { method: "POST", body: { email, otp } });

// signup with email + password. server emails a 6-digit code.
export const signup = ({ email, password, name }) =>
  request("/signup", { method: "POST", body: { email, password, name } });

// finish signup by submitting the OTP. returns JWT + user.
export const signupVerify = ({ email, otp }) =>
  request("/signup/verify", { method: "POST", body: { email, otp } });

export const loginWithPassword = ({ email, password }) =>
  request("/login", { method: "POST", body: { email, password } });

// swap a google credential (from GIS) for our own JWT.
// mode: "login" (reject unknown emails), "signup" (create-or-link), or undefined (legacy).
export const loginWithGoogle = ({ credential, mode }) =>
  request("/auth/google", { method: "POST", body: { credential, mode } });

// ------------------------------ tutor ------------------------------

export const getStats = () => request("/user/stats");

export const getTopics = () => request("/topics");

// catalogue of every question (id/title/topic/difficulty/tags only — the
// model solution is stripped server-side). Used by the sandbox picker.
export const getAllQuestions = () => request("/questions");

export const getAnalytics = () => request("/analytics");

export const getUserProgress = () => request("/user/progress");

export const getUserImprovement = () => request("/user/improvement");

// fetch next question, optional topic / difficulty / exclude list.
export const getNextQuestion = (opts = {}) => {
  const params = new URLSearchParams();
  params.set("topic", opts.topic ?? "all");
  if (opts.difficulty && opts.difficulty !== "all") params.set("difficulty", String(opts.difficulty));
  if (opts.excludeIds?.length) params.set("excludeIds", opts.excludeIds.join(","));
  if (opts.mode) params.set("mode", String(opts.mode));
  return request(`/questions/next?${params.toString()}`);
};

export const submitAnswer = ({ questionId, answer, hintsUsed, selfConfidence, mode }) =>
  request("/submit-answer", { method: "POST", body: { questionId, answer, hintsUsed, selfConfidence, mode } });

export const getHint = ({ questionId, level, mode }) => {
  const params = new URLSearchParams();
  params.set("level", String(level));
  if (mode) params.set("mode", String(mode));
  return request(`/questions/${questionId}/hint?${params.toString()}`);
};

// ------------------------------ sandbox ------------------------------

// list of supported languages (id, label, version, starter template).
export const getSandboxLanguages = () => request("/sandbox/languages");

// run user-written source in the chosen language. Backend proxies to Piston
// so we never execute untrusted code in our own process.
export const runCode = ({ language, source, stdin }) =>
  request("/sandbox/run", {
    method: "POST",
    body: { language, source, stdin: stdin || "" },
    timeoutMs: 30_000,
  });
