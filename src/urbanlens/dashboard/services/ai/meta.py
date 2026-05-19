"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    meta.py                                                                                            *
*        - Path:    /dashboard/services/ai/meta.py                                                                     *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-03-21                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-21     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
SHORTEST_MESSAGE = 50
MAX_TOKENS = 16000

PROJECT_DESCRIPTION = "" +\
    "You are an AI assistant for Urban Lens. This web application allows urban explorers " +\
    "to organize and share information about abandoned locations."

FORMATTING = "" +\
    "Place the relevant part of your answer inside <ANSWER> tags. The text inside the tag will " +\
    "be extracted and parsed by a script, so it needs to be formatted correctly and not contain any extra content. " +\
    "For example, if the task is to interpret a date from a string, and the string is 'Employee was paid on January 1, 2023', your reply should include " +\
    "<ANSWER>2023-01-01</ANSWER>, so that it can be easily parsed into a date object. " +\
    "If the task is to choose a category for a location, and the location is a church, your reply should include <ANSWER>church</ANSWER>. " +\
    "No other text should be included inside the answer tag."

INSTRUCTIONS = ""
