// Portalborn 한국어 패치 — 조사 자동 선택
// 번역문의 "{이름}을(를)" 같은 자리에서, 실제로 들어간 단어의 받침을 보고 을/를 하나만 남긴다.
// 패처가 index.html에 이 스크립트를 넣고, 게임의 문장 출구(gt, t, 로그)에서 부른다.
(function () {
  "use strict";
  var PICK = {
    "을(를)": ["을", "를"], "를(을)": ["을", "를"],
    "이(가)": ["이", "가"], "가(이)": ["이", "가"],
    "은(는)": ["은", "는"], "는(은)": ["은", "는"],
    "과(와)": ["과", "와"], "와(과)": ["과", "와"],
    "(이)라": ["이라", "라"], "(이)여": ["이여", "여"], "(아)야": ["아", "야"],
    "(이)나": ["이나", "나"], "(이)랑": ["이랑", "랑"], "(이)다": ["이다", "다"],
    "(이)며": ["이며", "며"], "(이)에요": ["이에요", "예요"], "(이)야": ["이야", "야"]
  };
  var QUICK = /[을를이가은는과와]\(|\((?:으|이|아)\)/;
  // 단어 끝 글자 + (태그·닫는 괄호) + (공백 하나) + 조사 표기
  // 조사 앞 공백은 지움: 게임 원문 틀이 "{이름} {문구}"처럼 띄어 놓은 자리에 조사로 시작하는 문구가 들어올 때
  var RE = /([가-힣A-Za-z0-9])((?:<[^<>]*>|[\])}"'”’〉》」』】）］*])*) ?(을\(를\)|를\(을\)|이\(가\)|가\(이\)|은\(는\)|는\(은\)|과\(와\)|와\(과\)|\(으\)로|\(이\)라|\(이\)여|\(아\)야|\(이\)나|\(이\)랑|\(이\)다|\(이\)며|\(이\)에요|\(이\)야)/g;
  // 숫자 끝자리 읽기: 0영 1일 2이 3삼 4사 5오 6육 7칠 8팔 9구 → [받침 있음, ㄹ 받침]
  var DIGIT = { "0": [1, 0], "1": [1, 1], "2": [0, 0], "3": [1, 0], "4": [0, 0], "5": [0, 0], "6": [1, 0], "7": [1, 1], "8": [1, 1], "9": [0, 0] };

  function finalOf(ch) {
    var c = ch.charCodeAt(0);
    if (c >= 0xAC00 && c <= 0xD7A3) {
      var jong = (c - 0xAC00) % 28;
      return [jong !== 0, jong === 8];
    }
    if (DIGIT[ch]) return [!!DIGIT[ch][0], !!DIGIT[ch][1]];
    var l = ch.toLowerCase();
    if (l === "l") return [true, true];
    if ("mnbckpt".indexOf(l) >= 0) return [true, false];
    return [false, false];
  }

  function fix(text) {
    if (typeof text !== "string" || text.indexOf("(") < 0 || !QUICK.test(text)) return text;
    return text.replace(RE, function (all, ch, mid, mark) {
      var f = finalOf(ch);
      var out;
      if (mark === "(으)로") out = f[0] && !f[1] ? "으로" : "로";
      else out = f[0] ? PICK[mark][0] : PICK[mark][1];
      return ch + mid + out;
    });
  }

  try {
    Object.defineProperty(globalThis, "__pbKoJosa", { value: fix, writable: false, configurable: false });
  } catch (_) {
    globalThis.__pbKoJosa = fix;
  }
})();
