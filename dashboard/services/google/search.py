"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    search.py                                                                                            *
*        Path:    /dashboard/services/google/search.py                                                                 *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-07                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-07     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from django.conf import settings
import requests
import logging
from icecream import ic
from dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

class GoogleCustomSearchGateway(Gateway):
    """
    Gateway for the Google Custom Search API.
    """

    def __init__(self):
        self.api_key = settings.GOOGLE_SEARCH_API_KEY
        self.cx = settings.GOOGLE_SEARCH_TENANT
        self.base_url = "https://customsearch.googleapis.com/customsearch/v1"

    def search(self, query : str, max_results : int = 20) -> dict:
        """
        Perform a search using the Google Custom Search API.
        """
        headers = {
            'Referer': 'http://localhost:8000'
        }
        params = {
            'key': self.api_key,
            'cx': self.cx,
            'q': query,
            #'num': min(max_results, 20)
        }
        response = requests.get(self.base_url, params=params, headers=headers)
        response.raise_for_status()
        return self.parse_response(response)

    def parse_response(self, response : requests.Response) -> dict:
        """
        Extract search results from the API response.
        """
        data = response.json()

        results = []
        for item in data.get('items', []):
            result = {
                'title': item.get('title'),
                'link': item.get('link'),
                'snippet': item.get('snippet')
            }
            results.append(result)
        return results
    
    """
    Sample response:
    {'_content': b'{
        "kind": "customsearch#search",
        "url": {
        "type": "a'
                b'pplication/json",
        "template": "https://www.googleapis.co'
                b'm/customsearch/v1?q={searchTerms}&num={count?}&start={startIndex'
                b'?}&lr={language?}&safe={safe?}&cx={cx?}&sort={sort?}&filter={fil'
                b'ter?}&gl={gl?}&cr={cr?}&googlehost={googleHost?}&c2coff={disable'
                b'CnTwTranslation?}&hq={hq?}&hl={hl?}&siteSearch={siteSearch?}&sit'
                b'eSearchFilter={siteSearchFilter?}&exactTerms={exactTerms?}&exclu'
                b'deTerms={excludeTerms?}&linkSite={linkSite?}&orTerms={orTerms?}&'
                b'dateRestrict={dateRestrict?}&lowRange={lowRange?}&highRange={hig'
                b'hRange?}&searchType={searchType}&fileType={fileType?}&rights={ri'
                b'ghts?}&imgSize={imgSize?}&imgType={imgType?}&imgColorType={imgCo'
                b'lorType?}&imgDominantColor={imgDominantColor?}&alt=json"
        }'
                b',
        "queries": {
        "request": [
            {
            "title": "'
                b'Google Custom Search - campus",
            "totalResults": "341'
                b'0000000",
            "searchTerms": "campus",
            "count": '
                b'10,
            "startIndex": 1,
            "inputEncoding": "utf8"'
                b',
            "outputEncoding": "utf8",
            "safe": "off",
    '
                b'       "cx": "85435ec2ee5bd45af"
            }
        ],
        "nextPag'
                b'e": [
            {
            "title": "Google Custom Search - campu'
                b's",
            "totalResults": "3410000000",
            "searchTer'
                b'ms": "campus",
            "count": 10,
            "startIndex": 11'
                b',
            "inputEncoding": "utf8",
            "outputEncoding":'
                b' "utf8",
            "safe": "off",
            "cx": "85435ec2ee5bd'
                b'45af"
            }
        ]
        },
        "context": {
        "title": "Ur'
                b'ban Lens"
        },
        "searchInformation": {
        "searchTime": 0.'
                b'225936,
        "formattedSearchTime": "0.23",
        "totalResults'
                b'": "3410000000",
        "formattedTotalResults": "3,410,000,000'
                b'"
        },
        "items": [
        {
            "kind": "customsearch#result'
                b'",
            "title": "Parents & Students \xc2\xb7 Infinite Campus'
                b'",
            "htmlTitle": "Parents &amp; Students \xc2\xb7 Infinit'
                b'e \\u003cb\\u003eCampus\\u003c/b\\u003e",
            "link": "htt'
                b'ps://www.infinitecampus.com/audience/parents-students",
        '
                b'  "displayLink": "www.infinitecampus.com",
            "snippet": '
                b'"Students, say hello to your new best friend. \xc2\xb7 STUDENT'
                b' FAVORITES: \xc2\xb7 Real-time notifications! Grade notificati'
                b'ons are sent immediately after they are entered.",
            "ht'
                b'mlSnippet": "Students, say hello to your new best friend. &middo'
                b't; STUDENT FAVORITES: &middot; Real-time notifications! Grade no'
                b'tifications are sent immediately after they are entered.",
    '
                b'     "cacheId": "XlpZHFMd9_cJ",
            "formattedUrl": "https'
                b'://www.infinitecampus.com/audience/parents-students",
            '
                b'"htmlFormattedUrl": "https://www.infinite\\u003cb\\u003ecampus'
                b'\\u003c/b\\u003e.com/audience/parents-students",
            "pagema'
                b'p": {
            "metatags": [
                {
                "viewpor'
                b't": "width=device-width, initial-scale=1.0"
                }
        '
                b'    ]
            }
        },
        {
            "kind": "customsearch#resul'
                b't",
            "title": "Campus.edu | A Community College for the'
                b' Future",
            "htmlTitle": "\\u003cb\\u003eCampus\\u003c/'
                b'b\\u003e.edu | A Community College for the Future",
            "li'
                b'nk": "https://campus.edu/",
            "displayLink": "campus.edu'
                b'",
            "snippet": "Campus is a new kind of community colle'
                b'ge where live online classes are taught by faculty who also teac'
                b'h at some of the top universities and HBCUs in the\xc2\xa0..."'
                b',
            "htmlSnippet": "\\u003cb\\u003eCampus\\u003c/b\\u003'
                b'e is a new kind of community college where live online classes a'
                b're taught by faculty who also teach at some of the top universit'
                b'ies and HBCUs in the&nbsp;...",
            "cacheId": "nLQrIMwszA'
                b'MJ",
            "formattedUrl": "https://campus.edu/",
            "htm'
                b'lFormattedUrl": "https://\\u003cb\\u003ecampus\\u003c/b\\u003e.e'
                b'du/",
            "pagemap": {
            "metatags": [
                {
    '
                b'           "next-head-count": "8",
                "viewport": "'
                b'width=device-width, initial-scale=1, maximum-scale=1, user-scala'
                b'ble=no"
                }
            ]
            }
        },
        {
        '
                b' "kind": "customsearch#result",
            "title": "CAMPUS USA C'
                b'redit Union: Home",
            "htmlTitle": "\\u003cb\\u003eCAMPUS\\'
                b'u003c/b\\u003e USA Credit Union: Home",
            "link": "https:'
                b'//campuscu.com/",
            "displayLink": "campuscu.com",
        '
                b' "snippet": "CAMPUS members own the credit union! See why we'
                b"'re better than a bank. ... Make an Appointment ... CAMPUS Repre"
                b'sentative. ... Open an Account. Open a checking or\xc2\xa0..."'
                b',
            "htmlSnippet": "\\u003cb\\u003eCAMPUS\\u003c/b\\u003'
                b'e members own the credit union! See why we&#39;re better than a '
                b'bank. ... Make an Appointment ... \\u003cb\\u003eCAMPUS\\u003c/'
                b'b\\u003e Representative. ... Open an Account. Open a checking'
                b' or&nbsp;...",
            "cacheId": "3OEGiGarjSsJ",
            "forma'
                b'ttedUrl": "https://campuscu.com/",
            "htmlFormattedUrl":'
                b' "https://\\u003cb\\u003ecampus\\u003c/b\\u003ecu.com/",
        '
                b'   "pagemap": {
            "cse_thumbnail": [
                {
        '
                b'       "src": "https://encrypted-tbn0.gstatic.com/images?q=tbn:A'
                b'Nd9GcT22jKdOwDZO37_WW6GbcNa3nwUektGpO758CdoJpjHot_CocdVs-i6v63B"'
                b',
                "width": "225",
                "height": "225"
                ...
    """