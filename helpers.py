

#%%
import os
from argparse import Namespace
from collections import Counter
import json
import re
import string

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm_notebook
import numpy as np
from sklearn.model_selection import train_test_split
#from ReviewVectorizer import ReviewVectorizer
from typing import Dict, List, Optional
from review_dataset import NewsDataset

model_path = NewsDataset.get_datapath(data_foldername="model_store", data_filename="model.pth")

vector_path = NewsDataset.get_datapath(data_foldername='model_store', data_filename='vectorizer.json')
data_path = NewsDataset.get_datapath(data_foldername="data_splitted", data_filename="review_df_split2.csv")

#%% setting

args = Namespace(
    # Data and Path hyper parameters
    data_csv=data_path,
    vectorizer_file=vector_path, #"vectorizer.json",
    model_state_file=model_path,
    save_dir="model_store",
    # Model hyper parameters
    glove_filepath='glove/glove.6B.100d.txt', 
    use_glove=False,
    embedding_size=100, 
    hidden_dim=100, 
    num_channels=100, 
    # Training hyper parameter
    seed=1337, 
    learning_rate=0.001, 
    dropout_p=0.1, 
    batch_size=128, 
    num_epochs=3, 
    early_stopping_criteria=5, 
    # Runtime option
    cuda=True, 
    catch_keyboard_interrupt=True, 
    reload_from_files=False,
    expand_filepaths_to_save_dir=True,
    device='cpu'
) 


   
#%% helper functions
def make_train_state(args):
    return {'stop_early': False,
            'early_stopping_step': 0,
            'early_stopping_best_val': 1e8,
            'learning_rate': args.learning_rate,
            'epoch_index': 0,
            'train_loss': [],
            'train_acc': [],
            'val_loss': [],
            'val_acc': [],
            'test_loss': -1,
            'test_acc': -1,
            'model_filename': args.model_state_file}

def update_train_state(args, model, train_state):
    """Handle the training state updates.

    Components:
     - Early Stopping: Prevent overfitting.
     - Model Checkpoint: Model is saved if the model is better

    :param args: main arguments
    :param model: model to train
    :param train_state: a dictionary representing the training state values
    :returns:
        a new train_state
    """

    # Save one model at least
    if train_state['epoch_index'] == 0:
        torch.save(model.state_dict(), train_state['model_filename'])
        train_state['stop_early'] = False

    # Save model if performance improved
    elif train_state['epoch_index'] >= 1:
        loss_tm1, loss_t = train_state['val_loss'][-2:]

        # If loss worsened
        if loss_t >= train_state['early_stopping_best_val']:
            # Update step
            train_state['early_stopping_step'] += 1
        # Loss decreased
        else:
            # Save the best model
            if loss_t < train_state['early_stopping_best_val']:
                torch.save(model.state_dict(), train_state['model_filename'])

            # Reset early stopping step
            train_state['early_stopping_step'] = 0

        # Stop early ?
        train_state['stop_early'] = \
            train_state['early_stopping_step'] >= args.early_stopping_criteria

    return train_state

def compute_accuracy(y_pred, y_target):
    _, y_pred_indices = y_pred.max(dim=1)
    n_correct = torch.eq(y_pred_indices, y_target).sum().item()
    return n_correct / len(y_pred_indices) * 100
        
        
        
#%% general ultilities
def set_seed_everywhere(seed, cuda):
    np.random.seed(seed)
    torch.manual_seed(seed)
    if cuda:
        torch.cuda.manual_seed_all(seed)
        
def handle_dirs(dirpath):
    if not os.path.exists(dirpath):
        os.makedirs(dirpath)
        
        
def load_glove_from_file(glove_filepath):
    """Load the glove embeddings
    
    Args:
        glove_filepath (str): path to the glove embedding file
        
    Returns:
        word_to_idx (dict): embeddings (numpy.ndarray)
    
    """
    word_to_index = {}
    embeddings = []
    
    with open(glove_filepath) as fp:
        for index, line in enumerate(fp):
            line = line.split(" ") # each line word num1 num2 ...
            word_to_index[line[0]] = index
            embedding_i = np.array([float(val) for val in line[1:]])
            embeddings.append(embedding_i)
    return word_to_index, np.stack(embeddings)
            
        
def make_embedding_matrix(glove_filepath, words):
    """create embedding matrix from a specific set of words
    
    Args:
        glove_fliepath (str): fle path to the glove embeddings
        words (list): list of words in the dataset
    
    """        
    word_to_idx, glove_embeddings = load_glove_from_file(glove_filepath)
    embedding_size = glove_embeddings.shape[1]
    
    final_embeddings = np.zeros((len(words), embedding_size))
    
    for i, word in enumerate(words):
        if word in word_to_idx:
            final_embeddings[i, :] = glove_embeddings[word_to_idx[word]]
        else:
            embedding_i = torch.ones(1, embedding_size)
            torch.nn.init.xavier_uniform_(embedding_i)
            final_embeddings[i, :] = embedding_i
            
    return final_embeddings


       
    


def generate_batches(dataset, batch_size, shuffle=True,
                     drop_last=True, device="cpu"): 
    """
    A generator function which wraps the PyTorch DataLoader. It will 
      ensure each tensor is on the write device location.
    """
    dataloader = DataLoader(dataset=dataset, batch_size=batch_size,
                            shuffle=shuffle, drop_last=drop_last)

    for data_dict in dataloader:
        out_data_dict = {}
        for name, tensor in data_dict.items():
            out_data_dict[name] = data_dict[name].to(device)
        yield out_data_dict
        
        
        
#%% Inference
# Preprocess the reviews
def preprocess_text(text):
    text = ' '.join(word.lower() for word in text.split(" "))
    text = re.sub(pattern=r"([.,!?])", repl=r" \1 ", string=text)
    text = re.sub(pattern=r"[^a-zA-Z.,!?]+", repl=r" ", string=text)       
    return text


#%%
def predict_category(title, classifier, vectorizer, max_length):
    """Predicts a news category for a new title
    
    Args:
        title (str): a raw title string
        classifier (NewsVectorizer): an instanve of the trained classifier
        vectorizer (NewsVectorizer): the corresponding vectorizer
        max_length (int): the max sequence length
            CNN are sensitive to the input data tensor size, 
            This ensures to keep it the same size as the training data
    """
    title = preprocess_text(title)
    vectorized_title = torch.tensor(vectorizer.vectorize(title, vector_length=max_length))
    result = classifier(vectorized_title.unsqueeze(0), apply_softmax=True)
    probability_values, indices = result.max(dim=1)
    predicted_category = vectorizer.category_vocab.lookup_index(indices.item())
    
    return {'category': predicted_category,
            'probability': probability_values.item()
            }











