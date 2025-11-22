import lxml.html
from lxml.html.clean import Cleaner
from typing import Tuple

class DOMPruner:
    def __init__(self):
        # Configure lxml Cleaner
        self.cleaner = Cleaner(
            scripts=True,
            javascript=True,
            style=True,
            links=True,
            meta=True,
            page_structure=False,  # Keep html, body, head
            processing_instructions=True,
            embedded=False, # Keep images? Maybe.
            frames=False,
            forms=False, # Keep forms
            annoying_tags=False,
            remove_unknown_tags=False,
            safe_attrs_only=False, # We will filter attributes manually or use safe_attrs
        )
        
        # Attributes to keep (allow-list approach is safer for "junk" removal)
        self.ALLOWED_ATTRS = {
            'id', 'name', 'type', 'placeholder', 'value', 'aria-label', 
            'aria-labelledby', 'aria-describedby', 'role', 'href', 'src', 'alt'
        }

    def estimate_tokens(self, text: str) -> int:
        """
        Fast heuristic for token count: ~4 chars per token.
        """
        return len(text) // 4

    def prune(self, html_content: str) -> Tuple[str, int]:
        """
        Cleans HTML and returns (cleaned_html, token_count).
        Truncates if > 10k tokens.
        """
        if not html_content:
            return "", 0

        try:
            # 1. Parse and Clean
            # lxml cleaner is fast and robust
            cleaned_html = self.cleaner.clean_html(html_content)
            
            # 2. Advanced Attribute Stripping (Manual Pass)
            # Cleaner doesn't easily support "strip all except X", so we iterate.
            root = lxml.html.fromstring(cleaned_html)
            
            for element in root.iter():
                # Remove comments
                if isinstance(element, lxml.html.HtmlComment):
                    element.drop_tree()
                    continue
                
                # Filter attributes
                attribs_to_remove = []
                for attr in element.attrib:
                    if attr not in self.ALLOWED_ATTRS:
                        attribs_to_remove.append(attr)
                
                for attr in attribs_to_remove:
                    del element.attrib[attr]

            # Serialize back to string
            final_html = lxml.html.tostring(root, encoding='unicode', pretty_print=True)
            
            # 3. Token Count & Truncation
            token_count = self.estimate_tokens(final_html)
            
            if token_count > 10000:
                # Truncate intelligently (this is a naive cut, but safe for now)
                # A better approach would be to remove children of deep nodes, but for MVP:
                trunc_len = 10000 * 4
                final_html = final_html[:trunc_len] + "... [TRUNCATED]"
                token_count = 10000

            return final_html, token_count

        except Exception as e:
            print(f"Error pruning DOM: {e}")
            # Fallback to raw or empty? Return raw but warn.
            return html_content[:1000], self.estimate_tokens(html_content[:1000])
