import { fromMarkdown } from "mdast-util-from-markdown";
import { gfm } from "micromark-extension-gfm";
import { gfmFromMarkdown } from "mdast-util-gfm";
export function parse(src) { return fromMarkdown(src, { extensions: [gfm()], mdastExtensions: [gfmFromMarkdown()] }); }
