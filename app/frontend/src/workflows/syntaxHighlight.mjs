import hljs from "highlight.js/lib/core";
import cpp from "highlight.js/lib/languages/cpp";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import json from "highlight.js/lib/languages/json";
import css from "highlight.js/lib/languages/css";
import xml from "highlight.js/lib/languages/xml";
import bash from "highlight.js/lib/languages/bash";
import powershell from "highlight.js/lib/languages/powershell";
import sql from "highlight.js/lib/languages/sql";
import csharp from "highlight.js/lib/languages/csharp";
import java from "highlight.js/lib/languages/java";
import rust from "highlight.js/lib/languages/rust";
import go from "highlight.js/lib/languages/go";
import yaml from "highlight.js/lib/languages/yaml";

for (const [name, grammar] of Object.entries({cpp,python,javascript,typescript,json,css,xml,bash,powershell,sql,csharp,java,rust,go,yaml})) hljs.registerLanguage(name,grammar);
const aliases = {"c++":"cpp",c:"cpp",h:"cpp",hpp:"cpp",js:"javascript",jsx:"javascript",ts:"typescript",tsx:"typescript",py:"python",sh:"bash",shell:"bash",ps1:"powershell",html:"xml",svg:"xml","c#":"csharp",cs:"csharp",rs:"rust",yml:"yaml"};
const escape = text => text.replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
export function highlightCode(content, language) {
  const text=String(content || "");
  const lang=aliases[String(language).toLowerCase()] || String(language).toLowerCase();
  if(text.length>80_000 || !hljs.getLanguage(lang)) return escape(text);
  try { return hljs.highlight(text,{language:lang,ignoreIllegals:true}).value; } catch { return escape(text); }
}
