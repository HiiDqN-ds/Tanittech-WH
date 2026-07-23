from django import template
from django.utils.translation import get_language_info

register = template.Library()


@register.simple_tag
def language_selector():
    """
    Returns HTML for a language selector dropdown.
    """
    from django.conf import settings
    
    languages = settings.LANGUAGES
    current_lang = get_language_info('de' if 'de' in settings.LANGUAGES else 'en')
    
    html = '<select onchange="switchLanguage(this.value)" class="language-selector px-2 py-1 border rounded text-sm bg-white">'
    
    for lang_code, lang_name in languages:
        selected = 'selected' if lang_code == current_lang else ''
        html += f'<option value="{lang_code}" {selected}>{lang_name}</option>'
    
    html += '</select>'
    
    return html


@register.simple_tag
def get_current_language():
    """Returns the current language code."""
    from django.utils import translation
    return translation.get_language()


@register.simple_tag
def language_button(lang_code):
    """
    Returns a button for switching to a specific language.
    """
    lang_info = get_language_info(lang_code)
    current = translation.get_language()
    is_active = 'bg-blue-600 text-white' if current == lang_code else 'bg-gray-200'
    
    return f'''
    <button onclick="switchLanguage('{lang_code}')" 
            class="px-3 py-1 rounded {is_active} text-sm font-medium transition">
        {lang_info['name_local']}
    </button>
    '''
