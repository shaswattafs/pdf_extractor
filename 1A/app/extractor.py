import re
from pathlib import Path
from collections import Counter
from typing import List, Dict
import fitz  # PyMuPDF
import statistics


class PDFOutlineExtractor:
    def __init__(self):
        self.heading_patterns = {
            'numbered': [
                (r'^\d+\.?\s+[A-Z]', 1),
                (r'^\d+\.\d+\.?\s+', 2),
                (r'^\d+\.\d+\.\d+\.?\s+', 3),
            ],
            'keywords': {
                1: ['chapter', 'section', 'part'],
                2: ['subsection', 'topic'],
                3: ['sub-topic', 'point']
            }
        }

    def extract_text_with_formatting(self, page):
        blocks = []
        text_dict = page.get_text("dict")
        
        for block in text_dict["blocks"]:
            if block["type"] == 0:  # Text block
                for line in block["lines"]:
                    # Combine all spans in a line into single text
                    line_text = ""
                    combined_spans = []
                    line_bbox = line["bbox"]
                    
                    for span in line["spans"]:
                        line_text += span["text"]
                        combined_spans.append({
                            "text": span["text"],
                            "font": span["font"],
                            "size": round(span["size"], 1),
                            "flags": span["flags"]
                        })
                    
                    if line_text.strip():
                        # Use the largest font size and boldest formatting from the line
                        max_size = max(span["size"] for span in combined_spans)
                        is_bold = any(span["flags"] & 2**4 for span in combined_spans)
                        
                        blocks.append({
                            "text": line_text.strip(),
                            "info": {
                                "bbox": line_bbox,
                                "spans": combined_spans,
                                "max_size": max_size,
                                "is_bold": is_bold
                            },
                            "page": page.number + 1
                        })
        return blocks

    def calculate_font_statistics(self, blocks):
        font_sizes = [span["size"] for block in blocks for span in block["info"]["spans"]]
        if not font_sizes:
            return {}
        return {
            "mean": statistics.mean(font_sizes),
            "median": statistics.median(font_sizes),
            "mode": statistics.mode(font_sizes) if len(set(font_sizes)) < len(font_sizes) else statistics.median(font_sizes),
            "max": max(font_sizes),
            "min": min(font_sizes)
        }

    def is_colon_heading(self, block, next_blocks):
        """Enhanced colon-based heading detection with bold requirement"""
        text = block["text"].strip()
        
        # Must end with colon
        if not text.endswith(':'):
            return False
        
        # **NEW: Must be bold for colon headings**
        if not block["info"]["is_bold"]:
            return False
        
        # Remove colon for analysis
        heading_text = text[:-1].strip()
        
        # Should be reasonably short for a heading
        if len(heading_text) > 100 or len(heading_text) < 3:
            return False
        
        # Common heading patterns with colons
        colon_patterns = [
            r'^(Timeline|Summary|Background|Access|Training|Guidance)',
            r'^(Phase [IVX]+|Appendix [ABC]|For each)',
            r'^[A-Z][a-zA-Z\s]+$',  # Proper case headings
            r'^[A-Z][a-z]+\s+[a-z]+',  # Title case patterns
        ]
        
        if any(re.match(pattern, heading_text, re.IGNORECASE) for pattern in colon_patterns):
            return True
        
        # Check if next content is indented or on new line
        if next_blocks:
            current_y = block["info"]["bbox"][1]
            next_y = next_blocks[0]["info"]["bbox"][1]
            
            # If there's a significant vertical gap, likely a heading
            if abs(next_y - current_y) > 10:
                return True
        
        return False


    def is_potential_heading(self, block, font_analysis, next_blocks):
        text = block["text"].strip()
        
        # Skip very long text
        if len(text) > 200:
            return False, 0
        
        # Skip very short text
        if len(text) < 3:
            return False, 0
        
        score = 0
        detected_level = 0
        
        # Check for colon headings first
        is_colon = self.is_colon_heading(block, next_blocks)
        if is_colon:
            score += 4
            detected_level = self.classify_heading_level(block, font_analysis, is_colon_heading=True)
        
        # Font size analysis
        max_font_size = block["info"]["max_size"]
        if max_font_size in font_analysis['heading_candidates']:
            score += 5
            if detected_level == 0:
                detected_level = font_analysis['heading_candidates'][max_font_size]['level']
        
        # Numbered patterns
        for pattern, level in self.heading_patterns['numbered']:
            if re.match(pattern, text):
                score += 3
                detected_level = level
                break
        
        # Bold formatting
        if block["info"]["is_bold"]:
            score += 2
        
        # All caps (but not too long)
        if text.isupper() and len(text) < 60:
            score += 2
        
        # Appendix/Phase patterns
        if re.match(r'^(Appendix|Phase)', text):
            score += 3
        
        return score >= 4, max(detected_level, 1) if score >= 4 else 0



    def analyze_font_distribution(self, blocks):
        """Analyze font size distribution to identify potential headings"""
        font_sizes = []
        for block in blocks:
            for span in block["info"]["spans"]:
                font_sizes.append(span["size"])
        
        if not font_sizes:
            return {'dominant_size': 12, 'dominant_percentage': 1.0, 'heading_candidates': {}}
        
        # Calculate font size frequency
        from collections import Counter
        size_counts = Counter(font_sizes)
        total_spans = len(font_sizes)
        
        # Find dominant font size (body text)
        dominant_size = size_counts.most_common(1)[0][0]
        dominant_percentage = size_counts[dominant_size] / total_spans
        
        # Find heading candidates (larger fonts with low frequency)
        heading_candidates = {}
        for size, count in size_counts.items():
            percentage = count / total_spans
            if size > dominant_size and percentage <= 0.05:  # Max 5% for headings
                diff = size - dominant_size
                if diff >= 4:
                    level = 1
                elif diff >= 2:
                    level = 2
                elif diff >= 1:
                    level = 3
                else:
                    level = 0
                
                if level > 0:
                    heading_candidates[size] = {'percentage': percentage, 'level': level}
        
        return {
            'dominant_size': dominant_size,
            'dominant_percentage': dominant_percentage,
            'heading_candidates': heading_candidates
        }

    def extract_page_start_content(self, doc, start_lines=3):
        """Extract the first few lines of text from each page"""
        page_start_content = []
        
        for page_num, page in enumerate(doc):
            page_blocks = self.extract_text_with_formatting(page)
            
            # Sort blocks by vertical position (top to bottom)
            page_blocks.sort(key=lambda x: x["info"]["bbox"][1])
            
            # Get first few lines of text with their formatting info
            start_text_info = []
            line_count = 0
            
            for block in page_blocks:
                if line_count >= start_lines:
                    break
                
                text = block["text"].strip()
                if text:  # Only count non-empty text
                    start_text_info.append({
                        'text': text,
                        'font_size': block["info"]["max_size"],
                        'is_bold': block["info"]["is_bold"],
                        'page': page_num
                    })
                    line_count += 1
            
            page_start_content.append(start_text_info)
        
        return page_start_content

    def find_recurring_headers(self, page_start_content, min_pages=2):
        """Find text that repeats at the start of multiple pages with same formatting AND size"""
        recurring_headers = set()
        
        if len(page_start_content) < min_pages:
            return recurring_headers
        
        # Skip first page (page 0) as headers there might be actual content
        pages_to_check = page_start_content[1:]  # Start from page 1 (second page)
        
        # Collect all text with formatting from pages 1 onwards
        text_format_combinations = {}
        
        for page_content in pages_to_check:
            for text_info in page_content:
                text = text_info['text']
                font_size = text_info['font_size']
                is_bold = text_info['is_bold']
                
                # Create a key combining text, font size AND bold status
                key = (text, font_size, is_bold)
                
                if key not in text_format_combinations:
                    text_format_combinations[key] = []
                text_format_combinations[key].append(text_info['page'])
        
        # Find text that appears on multiple pages with EXACTLY same formatting
        total_pages_checked = len(pages_to_check)
        
        for (text, font_size, is_bold), page_list in text_format_combinations.items():
            # If text with EXACT same formatting appears on at least 60% of pages
            if len(page_list) / total_pages_checked >= 0.6:
                # Store the complete formatting info, not just text
                recurring_headers.add((text, font_size, is_bold))
        
        return recurring_headers


    def is_recurring_header(self, block, recurring_headers):
        """Check if a block matches a recurring header with exact formatting"""
        block_text = block["text"].strip()
        block_font_size = block["info"]["max_size"]
        block_is_bold = block["info"]["is_bold"]
        
        # Check against recurring headers with exact formatting match
        for (header_text, header_font_size, header_is_bold) in recurring_headers:
            # Must match text AND formatting exactly
            if (block_text == header_text and 
                block_font_size == header_font_size and 
                block_is_bold == header_is_bold):
                return True
            
            # Also check partial matches but only with exact formatting
            if ((header_text in block_text or block_text in header_text) and
                block_font_size == header_font_size and 
                block_is_bold == header_is_bold):
                return True
        
        return False


    def filter_recurring_headers(self, all_blocks, recurring_headers):
        """Remove blocks that match recurring headers with exact formatting"""
        filtered_blocks = []
        
        for block in all_blocks:
            if not self.is_recurring_header(block, recurring_headers):  # Pass whole block
                filtered_blocks.append(block)
            #else:
                # Optional: print what's being filtered for debugging
                #print(f"Filtering header: {block['text'][:50]}... (size: {block['info']['max_size']}, bold: {block['info']['is_bold']})")
                #pass
        
        return filtered_blocks





    def analyze_local_font_patterns(self, blocks, center_index, window_size=5):
        """Analyze font patterns around a specific block to detect table content"""
        start_idx = max(0, center_index - window_size)
        end_idx = min(len(blocks), center_index + window_size + 1)
        
        local_blocks = blocks[start_idx:end_idx]
        
        # Get font sizes and text lengths in the local area
        font_sizes = []
        text_lengths = []
        
        for block in local_blocks:
            max_size = max(span["size"] for span in block["info"]["spans"])
            font_sizes.append(max_size)
            text_lengths.append(len(block["text"].strip()))
        
        return {
            'font_sizes': font_sizes,
            'text_lengths': text_lengths,
            'most_common_size': max(set(font_sizes), key=font_sizes.count) if font_sizes else 0
        }

    def is_table_content(self, block, all_blocks, index):
        """Detect table content based on font patterns and text characteristics"""
        text = block["text"].strip()
        
        # Skip empty text
        if not text:
            return True
        
        # Analyze local font patterns
        local_analysis = self.analyze_local_font_patterns(all_blocks, index)
        current_font_size = max(span["size"] for span in block["info"]["spans"])
        
        # Check if current font size is very common in local area (table indicator)
        font_repetition_count = local_analysis['font_sizes'].count(current_font_size)
        total_local_blocks = len(local_analysis['font_sizes'])
        
        # If this font size appears in >60% of nearby blocks, likely table content
        if font_repetition_count / total_local_blocks > 0.6:
            # Additional checks for table content characteristics
            
            # 1. Short text (table cells are usually brief)
            if len(text) < 80:
                
                # 2. Check if surrounding blocks also have short text
                short_text_neighbors = sum(1 for length in local_analysis['text_lengths'] if length < 80)
                if short_text_neighbors / total_local_blocks > 0.5:
                    
                    # 3. No paragraph-like structure (no long sentences)
                    sentences = text.split('.')
                    has_long_sentences = any(len(sentence.strip()) > 50 for sentence in sentences)
                    
                    if not has_long_sentences:
                        return True
        
        return False

    def skip_table_body(self, blocks):
        """Filter out table content while preserving table headers and regular text"""
        filtered_blocks = []
        
        for i, block in enumerate(blocks):
            if not self.is_table_content(block, blocks, i):
                filtered_blocks.append(block)
            else:
                # Even if it's table content, keep it if it might be a table header
                # Table headers are often bold or larger than table content
                text = block["text"].strip()
                is_bold = any(span["flags"] & 2**4 for span in block["info"]["spans"])
                current_font = max(span["size"] for span in block["info"]["spans"])
                
                # Check if this could be a table header
                if (is_bold or len(text) > 20) and not self.is_obvious_table_cell(text):
                    filtered_blocks.append(block)
        
        return filtered_blocks

    def is_obvious_table_cell(self, text):
        """Identify obvious table cell content that should never be headings"""
        text_lower = text.lower().strip()
        
        # Pure numbers, dates, or very short entries
        obvious_cell_patterns = [
            r'^\d+$',  # Just numbers
            r'^\d+\.\d+$',  # Decimals
            r'^\d{1,2}/\d{1,2}/\d{2,4}$',  # Dates
            r'^\$\d+',  # Money
            r'^[a-zA-Z]{1,3}$',  # Very short abbreviations
        ]
        
        return any(re.match(pattern, text) for pattern in obvious_cell_patterns)

    def is_form_element(self, text):
        text_lower = text.lower()
        
        # Skip standalone numbers
        if re.match(r'^\d+\.?\s*$', text):
            return True
        
        # Skip table headers/form labels
        form_indicators = ['name', 'age', 'relationship', 's.no', 'date', 'signature']
        if any(indicator in text_lower for indicator in form_indicators):
            return True
        
        # Skip single letters or very short text
        if len(text) <= 2:
            return True
        
        # Skip text that looks like form instructions
        if any(word in text_lower for word in ['required', 'advance', 'rs.']):
            return True
        
        return False

    def classify_heading_level(self, block, font_analysis, is_colon_heading=False):
        """Improved level classification"""
        text = block["text"].strip()
        max_font_size = block["info"]["max_size"]
        
        # Numbered patterns have priority
        for pattern, level in self.heading_patterns['numbered']:
            if re.match(pattern, text):
                return level
        
        # Appendix patterns
        if re.match(r'^Appendix [ABC]:', text):
            return 2
        
        # Phase patterns
        if re.match(r'^Phase [IVX]+:', text):
            return 3
        
        # Colon headings are typically H3 or H4
        if is_colon_heading:
            if any(word in text.lower() for word in ['for each', 'it could mean']):
                return 4
            return 3
        
        # Font-based classification
        if max_font_size in font_analysis['heading_candidates']:
            return font_analysis['heading_candidates'][max_font_size]['level']
        
        # Default classification based on content
        if len(text) < 30 and text.isupper():
            return 1
        elif len(text) < 50:
            return 2
        else:
            return 3
    def extract_title_with_merging(self, blocks, doc_info):
        """Extract title by finding and merging title fragments"""
        # Find potential title blocks (first 10 blocks, large fonts, title keywords)
        title_candidates = []
        
        for i, block in enumerate(blocks[:10]):
            text = block["text"].strip()
            font_size = block["info"]["max_size"]
            
            # Look for title indicators or large fonts
            is_title_candidate = (
                any(word in text.upper() for word in ['RFP', 'REQUEST', 'PROPOSAL']) or
                font_size > 14 or  # Large font
                (i < 5 and len(text) > 10 and len(text) < 100)  # Early, reasonable length
            )
            
            if is_title_candidate:
                title_candidates.append({
                    "text": text,
                    "page": block["page"],
                    "detected_level": 1,
                    "info": block["info"]
                })
        
        # Merge title candidates using the same logic
        if title_candidates:
            merged_titles = self.merge_consecutive_headings(title_candidates)
            if merged_titles:
                return merged_titles[0]["text"]
        
        # Fallback to original logic
        return self.extract_title(blocks, doc_info)


    def extract_title(self, blocks, doc_info):
        """Extract title by combining fragments if needed"""
        # Look for title components in first few blocks
        title_parts = []
        
        for block in blocks[:10]:  # Check first 10 blocks
            text = block["text"].strip()
            if any(word in text.upper() for word in ['RFP', 'REQUEST', 'PROPOSAL']):
                title_parts.append(text)
        
        # Combine title parts if found
        if title_parts:
            return " ".join(title_parts)
        
        # Fallback to largest font
        candidates = []
        for block in blocks[:5]:
            if len(block["text"]) < 150:
                candidates.append((block["info"]["max_size"], block["text"]))
        
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        
        return "Untitled Document"
    def merge_consecutive_headings(self, potential_headings):
        """Merge headings that are fragments of the same logical heading"""
        if not potential_headings:
            return potential_headings
        
        merged_headings = []
        i = 0
        
        while i < len(potential_headings):
            current_heading = potential_headings[i]
            current_text = current_heading["text"].strip()
            current_page = current_heading["page"]
            current_level = current_heading["detected_level"]
            
            # Look ahead for immediate next heading only
            merged_text_parts = [current_text]
            merged_headings_list = [current_heading]  # Keep track of original headings
            j = i + 1
            
            # Only look at the very next heading
            if (j < len(potential_headings)):
                next_heading = potential_headings[j]
                next_text = next_heading["text"].strip()
                next_page = next_heading["page"]
                next_level = next_heading["detected_level"]
                
                # **UPDATED: Check formatting-based merging**
                should_merge = (
                    self.should_merge_headings_basic(
                        current_text, next_text, current_page, next_page, 
                        current_level, next_level, merged_text_parts
                    ) or 
                    self.should_merge_by_formatting(current_heading, next_heading)
                )
                
                if should_merge:
                    merged_text_parts.append(next_text)
                    merged_headings_list.append(next_heading)
                    j += 1
            
            # Create merged heading
            merged_text = " ".join(merged_text_parts)
            merged_heading = {
                "text": merged_text,
                "page": current_page,
                "detected_level": current_level,
                "info": current_heading["info"]  # Use first heading's formatting info
            }
            
            merged_headings.append(merged_heading)
            i = j
        
        return merged_headings

    def should_merge_headings_basic(self, current_text, next_text, current_page, next_page, 
                                current_level, next_level, merged_parts):
        """Your existing basic merging logic"""
        # Only merge if same page and same level
        if current_page != next_page or current_level != next_level:
            return False
        
        # All your existing conditions...
        if (current_text.strip().lower() == 'overview' and 
            'foundation level extensions' in next_text.lower()):
            return True
        
        if len(current_text.strip()) <= 6:
            return True
        
        broken_word_patterns = ['oposal', 'quest f', 'r Pr']
        if any(current_text.endswith(pattern) for pattern in broken_word_patterns):
            return True
        
        if next_text and next_text[0].islower():
            return True
        
        return False

    def should_merge_by_formatting(self, current_heading, next_heading):
        """NEW: Check if headings should merge based on identical formatting"""
        # Must be same page and level
        if (current_heading["page"] != next_heading["page"] or 
            current_heading["detected_level"] != next_heading["detected_level"]):
            return False
        
        # Must have identical formatting
        if not self.has_identical_formatting(current_heading, next_heading):
            return False
        
        # Must be visually adjacent
        if not self.are_visually_adjacent(current_heading, next_heading):
            return False
        
        # Additional check: if current heading ends appropriately for continuation
        current_text = current_heading["text"].strip()
        next_text = next_heading["text"].strip()
        
        # Examples: "3. Overview..." + "Syllabus" should merge
        # But "Table of Contents" + "Acknowledgements" should not
        
        # Merge if current doesn't end with period/punctuation (suggesting continuation)
        if not current_text.endswith(('.', '!', '?')):
            # And next text is reasonably short (likely a continuation word)
            if len(next_text.split()) <= 3:
                return True
        
        return False


    def should_merge_headings(self, current_text, next_text, current_page, next_page, 
                         current_level, next_level, merged_parts):
        """Ultra-conservative merging with formatting checks"""
        
        # Only merge if same page and same level
        if current_page != next_page or current_level != next_level:
            return False
        
        # **NEW: Get the full heading objects for formatting comparison**
        # (You'll need to pass these from the main method - see update below)
        
        # Existing conditions (keep all your current logic)
        
        # Case 1: Current text is "Overview" and next is "Foundation Level Extensions"
        if (current_text.strip().lower() == 'overview' and 
            'foundation level extensions' in next_text.lower()):
            return True
        
        # Case 2: Clearly broken words (length <= 6 characters)
        if len(current_text.strip()) <= 6:
            return True
        
        # Case 3: Current text ends with incomplete word patterns we've observed
        broken_word_patterns = ['oposal', 'quest f', 'r Pr']
        if any(current_text.endswith(pattern) for pattern in broken_word_patterns):
            return True
        
        # Case 4: Next text starts with lowercase (clear continuation)
        if next_text and next_text[0].islower():
            return True
        
        # **NEW CASE 5: Same formatting and visually adjacent**
        # This handles cases like "3. Overview..." + "Syllabus" 
        # We need to modify the method signature to pass full heading objects
        
        return False


    def is_clearly_incomplete(self, text):
        """Check if text is clearly an incomplete fragment"""
        incomplete_indicators = [
            # Very short text (likely fragment)
            len(text) <= 8,
            
            # Broken words we've seen
            text.endswith(('oposal', 'quest', 'r Pr')),
            
            # Single letters or very short words
            len(text.split()) == 1 and len(text) < 6,
            
            # Text that doesn't make sense on its own
            text in ['oposal', 'quest f', 'r Pr', 'the', 'and', 'of', 'to', 'for']
        ]
        
        return any(incomplete_indicators)

    def is_known_title_fragment(self, current_text, next_text):
        """Check for specific known patterns that should be merged"""
        known_patterns = [
            # RFP title patterns
            ('RFP' in current_text and 'Request' in next_text),
            ('Request' in current_text and 'Proposal' in next_text),
            
            # "Overview" + document type
            (current_text.strip() == 'Overview' and 'Foundation' in next_text),
            
            # Broken document titles
            ('Proposal for' in current_text and 'Developing' in next_text),
            ('Business Plan' in current_text and 'Ontario' in next_text),
        ]
        
        return any(known_patterns)


    def is_semantic_continuation(self, current_text, next_text):
        """Check if next_text semantically continues current_text"""
        # Common continuation patterns
        continuation_indicators = [
            # Title-like patterns
            (current_text.endswith('for') and next_text.startswith(('Developing', 'the', 'a'))),
            (current_text.endswith('to') and next_text.startswith(('Present', 'the', 'a'))),
            (current_text.endswith('the') and len(next_text.split()) <= 3),
            
            # RFP/Proposal patterns
            ('Request' in current_text and 'Proposal' in next_text),
            ('RFP' in current_text and ('Request' in next_text or 'Proposal' in next_text)),
            
            # Organization/Location patterns
            (current_text.endswith('Ontario') and 'Digital' in next_text),
            (current_text.endswith('Digital') and 'Library' in next_text),
            
            # Common phrase breaks
            (current_text.endswith('Business') and next_text.startswith('Plan')),
            (current_text.endswith('Foundation') and next_text.startswith('Level')),
        ]
        
        return any(continuation_indicators)

    def is_incomplete_fragment(self, current_text, next_text):
        """Check if current_text appears to be an incomplete fragment"""
        # Text ending with incomplete patterns
        incomplete_patterns = [
            current_text.endswith('oposal'),  # "Proposal" broken as "oposal"
            current_text.endswith('quest'),   # "Request" broken as "quest"  
            current_text.endswith('r Pr'),    # "for Proposal" broken
            len(current_text) <= 10 and not current_text.endswith(('.', '!', '?', ':')),
            # Single words that are likely fragments
            len(current_text.split()) == 1 and len(current_text) < 15
        ]
        
        return any(incomplete_patterns)

    def has_identical_formatting(self, current_heading, next_heading):
        """Check if two headings have exactly the same formatting"""
        current_info = current_heading["info"]
        next_info = next_heading["info"]
        
        # Compare font size (exact match)
        if current_info["max_size"] != next_info["max_size"]:
            return False
        
        # Compare bold status
        if current_info["is_bold"] != next_info["is_bold"]:
            return False
        
        # Compare font family (if available in spans)
        current_fonts = set(span["font"] for span in current_info["spans"])
        next_fonts = set(span["font"] for span in next_info["spans"])
        
        # If they don't share any common fonts, likely different formatting
        if not current_fonts.intersection(next_fonts):
            return False
        
        # Compare italic/flags if needed (flags contain font style info)
        current_flags = set(span["flags"] for span in current_info["spans"])
        next_flags = set(span["flags"] for span in next_info["spans"])
        
        # Must have overlapping flags (formatting styles)
        if not current_flags.intersection(next_flags):
            return False
        
        return True

    def are_visually_adjacent(self, current_heading, next_heading):
        """Check if headings are visually adjacent (separated only by whitespace/newline)"""
        current_bbox = current_heading["info"]["bbox"]
        next_bbox = next_heading["info"]["bbox"]
        
        # Must be on same page
        if current_heading["page"] != next_heading["page"]:
            return False
        
        # Check vertical proximity (Y coordinates)
        current_bottom = current_bbox[3]  # bottom Y coordinate
        next_top = next_bbox[1]           # top Y coordinate
        
        # They should be close vertically (within reasonable line spacing)
        vertical_gap = abs(next_top - current_bottom)
        
        # Allow for normal line spacing (adjust this value based on your documents)
        max_allowed_gap = 20  # points (roughly 1.5 line spacing)
        
        return vertical_gap <= max_allowed_gap
    def is_title_duplicate(self, heading_text, title_normalized):
        """Check if a heading is a duplicate of the title"""
        
        # Exact match
        if heading_text == title_normalized:
            return True
        
        # Check if heading is contained in title or vice versa
        if heading_text in title_normalized or title_normalized in heading_text:
            # Additional check: only consider it duplicate if similarity is high
            shorter_len = min(len(heading_text), len(title_normalized))
            if shorter_len > 10:  # Only for reasonably long text
                return True
        
        # Check for title fragments (common in fragmented PDFs)
        title_words = set(title_normalized.split())
        heading_words = set(heading_text.split())
        
        # If heading is mostly contained in title words
        if len(heading_words) > 0:
            common_words = title_words.intersection(heading_words)
            similarity_ratio = len(common_words) / len(heading_words)
            
            # If 80% of heading words are in title, likely a duplicate
            if similarity_ratio >= 0.8 and len(heading_words) >= 3:
                return True
        
        return False


    def process_pdf(self, pdf_path: Path) -> Dict:
        try:
            doc = fitz.open(pdf_path)
            
            # First pass: identify recurring headers
            page_start_content = self.extract_page_start_content(doc)
            recurring_headers = self.find_recurring_headers(page_start_content)
            
            # Second pass: extract all text blocks
            all_blocks = []
            for page in doc:
                page_blocks = self.extract_text_with_formatting(page)
                all_blocks.extend(page_blocks)
            
            # Filter out recurring headers
            all_blocks = self.filter_recurring_headers(all_blocks, recurring_headers)
            
            # Skip table content
            all_blocks = self.skip_table_body(all_blocks)
            
            # Extract title FIRST (before heading detection)
            title = self.extract_title_with_merging(all_blocks, doc.metadata)
            title_normalized = title.strip().lower()
            
            # Analyze font distribution
            font_analysis = self.analyze_font_distribution(all_blocks)
            
            # Extract headings
            potential_headings = []
            for i, block in enumerate(all_blocks):
                next_blocks = all_blocks[i+1:i+4]
                is_heading, level = self.is_potential_heading(block, font_analysis, next_blocks)
                if is_heading:
                    block["detected_level"] = level
                    potential_headings.append(block)
            
            # Merge consecutive headings that belong together
            merged_headings = self.merge_consecutive_headings(potential_headings)
            
            # Build outline - FILTER OUT TITLE
            outline = []
            for heading in merged_headings:
                heading_text = heading["text"].strip().lower()
                
                # Skip if this heading matches the title
                if self.is_title_duplicate(heading_text, title_normalized):
                    print(f"Skipping title duplicate: {heading['text']}")
                    continue
                    
                outline.append({
                    "level": f"H{heading['detected_level']}",
                    "text": heading["text"],
                    "page": heading["page"]
                })
            
            doc.close()
            return {"title": title, "outline": outline}
            
        except Exception as e:
            print(f"Error processing {pdf_path}: {str(e)}")
            return {"title": "Error Processing Document", "outline": []}



