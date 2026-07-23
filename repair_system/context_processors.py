from django.utils import translation

def language_context(request):
    """
    Context processor to make the current language available to all templates.
    """
    # Get language from cookie
    lang = request.COOKIES.get('django_language')
    
    # Validate language is in our supported languages
    if lang not in ['en', 'de']:
        lang = 'en'
    
    # Activate the language
    translation.activate(lang)
    
    return {
        'request_language': lang,
    }
